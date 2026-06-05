"""Telegram handlers, whitelist enforcement, and onboarding conversation.

Access control is **structural**: the whitelist is applied as a
``filters.User`` instance on every ``CommandHandler`` / ``MessageHandler``
registration (see :func:`register_handlers`). PTB silently drops updates
that don't match — which is intentional per the security review's
"neutral rejection is stealthier" recommendation. The only handler the
structural filter cannot reach is the ``CallbackQueryHandler`` for the
district keyboard; that one is wrapped in :func:`_whitelist_callback`.

Exceptions raised by any handler propagate to the global error handler
(:func:`global_error_handler`), which logs with ``logger.exception`` and
sends a generic apology to the user. Handlers themselves contain no
try/except for "unknown failure".

DB access goes through :func:`_locked_db`, an async context manager that
acquires the shared ``asyncio.Lock`` before yielding the sqlite
connection. Every site that touches ``conn`` runs inside that lock — the
Phase 1 plan promised this serialisation and Phase 4 wires it up.

The on-demand backfill on ``/watch`` blocks the handler (option A from
the Phase 4 spec): we await ``scraper.crawl`` for the user's districts
and send at most 15 bookable matches as individual messages, one per
course. The scrape itself runs outside the DB lock (it does no SQL); the
subscription insert and the per-message ``record_notification`` calls
run inside. If the crawl raises, the subscription is already saved, so
we tell the user the backfill was interrupted and let them re-trigger.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from vhsbot import scraper
from vhsbot._app_state import BD_CLIENT, BD_SCAN_RUNNING, BD_SETTINGS, locked_db
from vhsbot.config import Settings
from vhsbot.db import (
    BOOKABLE_AVAILABILITY,
    add_subscription,
    get_user_settings,
    list_subscriptions,
    record_notification,
    remove_subscription,
    set_paused,
    upsert_user_settings,
)
from vhsbot.formatting import course_card
from vhsbot.jobs import daily_scan as _daily_scan
from vhsbot.matching import matches
from vhsbot.parser import parse_district_map, parse_district_names

logger = logging.getLogger(__name__)

# Conversation states
STATE_PICK_DISTRICTS = 1
STATE_PICK_KEYWORD = 2

# Per-handler cap on the on-demand backfill. The cross-day 15-msg cap is
# deferred to Phase 5 (the daily-scan code path); see tasks/todo .md.
BACKFILL_CAP = 15

# User-facing note appended to the backfill completion message when
# ``scraper.crawl`` reports ``truncated=True``. Lifted to module-level so the
# tests can import and match against the exact string rather than a fragile
# "contains page-limit" substring assertion.
_TRUNCATION_NOTE = " Note: the scan hit the page limit; some matches may not have been captured."

# user_data keys (per-user state inside the onboarding ConversationHandler)
_UD_DISTRICT_MAP = "district_map"  # dict[int, int]
_UD_DISTRICT_NAMES = "district_names"  # dict[int, str]
_UD_SELECTED_DISTRICTS = "selected_districts"  # set[int]


# ---------------------------------------------------------------------------
# Pure helpers (testable without Telegram or DB)
# ---------------------------------------------------------------------------


def escape_markdown_v2(s: str) -> str:
    """Escape every Telegram Markdown V2 reserved character in ``s``.

    Thin wrapper around PTB's ``telegram.helpers.escape_markdown`` so we
    track upstream behaviour (e.g. if Telegram adds a new reserved char,
    PTB will roll it in). PTB's helper also escapes backslash, which is
    correct: a raw ``\\X`` in the message body would otherwise be parsed
    by Telegram as a deliberate escape of ``X``.
    """
    return escape_markdown(s, version=2)


def build_help_text() -> str:
    """Static help string. Plain text, MD-V2-safe by virtue of escaping."""
    lines = [
        "vhs-berlin-bot — commands:",
        "",
        "/start — first-time setup or show your current subs",
        "/help — this message",
        "/list — show your keywords and districts",
        "/watch <keyword> — add a keyword (runs an on-demand backfill)",
        "/unwatch <keyword> — remove a keyword",
        "/districts — re-pick your districts",
        "/pause — mute daily notifications",
        "/resume — un-mute daily notifications",
        "/scan — trigger a manual scan now",
    ]
    return escape_markdown_v2("\n".join(lines))


def build_list_text(keywords: list[str], districts: list[int], paused: bool) -> str:
    """Markdown V2 formatted summary of the user's subscription state."""
    kw_block = ", ".join(keywords) if keywords else "(none yet — use /watch)"
    dist_block = ", ".join(str(d) for d in sorted(districts)) if districts else "(none)"
    status = "PAUSED" if paused else "active"
    plain = f"Status: {status}\nKeywords: {kw_block}\nDistricts: {dist_block}"
    return escape_markdown_v2(plain)


def build_district_keyboard(
    district_map: dict[int, int],
    district_names: dict[int, str],
    selected: set[int],
) -> InlineKeyboardMarkup:
    """Render the multi-select district keyboard.

    Layout: 3 columns of district toggles in ascending district-id order,
    plus a final row with "All" (select all) and "Done" (confirm).
    Selected districts show a leading ``[x]`` marker — no emojis per the
    Phase 4 spec.

    Button text is the human-readable Bezirk name from ``district_names``;
    if a name is missing (defensive — should not happen when both maps
    come from the same form fetch) we fall back to the numeric district id
    so onboarding still works rather than crashing on a ``KeyError``.
    Callback data stays keyed by the integer district id so the toggle
    handler keeps its compact wire format.
    """
    sorted_ids = sorted(district_map.keys())
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for district_id in sorted_ids:
        marker = "[x] " if district_id in selected else ""
        name = district_names.get(district_id, str(district_id))
        label = f"{marker}{name}"
        current_row.append(InlineKeyboardButton(label, callback_data=f"toggle:{district_id}"))
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [
            InlineKeyboardButton("All", callback_data="all"),
            InlineKeyboardButton("Done", callback_data="done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Whitelist enforcement (callbacks only — slash/message handlers use
# ``filters.User`` configured in :func:`register_handlers`).
# ---------------------------------------------------------------------------


_HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def _whitelist_callback(fn: _HandlerFn) -> _HandlerFn:
    """Reject non-whitelisted users hitting a ``CallbackQueryHandler``.

    ``CallbackQueryHandler`` does not accept a ``filters=`` kwarg in
    PTB v22, so structural enforcement is not available. This decorator
    answers the inline callback with a neutral "Not authorised." alert
    and returns ``ConversationHandler.END`` so the conversation, if one
    is active, exits cleanly. Exceptions propagate to the global error
    handler.
    """

    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        settings: Settings = context.bot_data[BD_SETTINGS]
        user = update.effective_user
        if user is None or user.id not in settings.allowed_user_ids:
            logger.info(
                "rejecting callback from non-whitelisted user_id=%s",
                user.id if user else None,
            )
            if update.callback_query is not None:
                await update.callback_query.answer("Not authorised.", show_alert=True)
            return ConversationHandler.END
        return await fn(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single sink for every unhandled exception in a handler/job.

    Logs the exception with full traceback and (best-effort) sends a
    generic apology to the user. Never raises.
    """
    logger.exception("unhandled handler error: update=%r", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Something went wrong; please try again.",
            )
        except Exception:  # pragma: no cover  (best-effort apology)
            logger.exception("failed to send fallback apology message")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data[BD_SETTINGS]


def _client(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    return context.bot_data[BD_CLIENT]


# DB-lock helper lives in ``_app_state`` so jobs.py + handlers.py share it
# without a circular import. Alias ``_locked_db = locked_db`` keeps the
# existing call sites stable.
_locked_db = locked_db


async def _fetch_district_data(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[dict[int, int], dict[int, str]]:
    """Fetch the search form once and parse both district maps from it.

    Returns ``(checkbox_map, names_map)``:

    * ``checkbox_map`` — ``district_id -> checkbox_index`` for POST bodies.
    * ``names_map``    — ``district_id -> human-readable Bezirk name`` for
      rendering the inline-keyboard button labels.

    Both maps come from a single GET so we never split the request budget
    or risk the two views drifting out of sync. The sibling helper in
    :mod:`vhsbot.jobs` keeps its leaner ``parse_district_map``-only path
    because the daily scan doesn't need the names.

    Wraps the GET in try/except + ``raise_for_status`` so the caller can
    distinguish "site down" from "we have a bug" and surface a more
    informative message to the user.
    """
    try:
        resp = await client.get(settings.search_url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("district-map fetch failed: %s", e)
        raise
    return parse_district_map(resp.content), parse_district_names(resp.content)


async def _safe_fetch_district_data(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[dict[int, int], dict[int, str]] | None:
    """Wrapper around :func:`_fetch_district_data` that surfaces a friendly
    "site down" message and returns ``None`` instead of bubbling up the
    network error. Callers branch on ``None`` to short-circuit cleanly.
    """
    try:
        return await _fetch_district_data(_client(context), _settings(context))
    except httpx.HTTPError:
        if update.message is not None:
            await update.message.reply_text(
                "VHS Berlin appears to be down — please try again in a moment."
            )
        return None


async def _run_backfill(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    keyword: str,
) -> int:
    """Crawl the user's districts and send up to ``BACKFILL_CAP`` matches.

    Returns the number of messages actually sent. Each send is logged in
    ``notification_log`` with reason ``"backfill"``.

    If ``scraper.crawl`` raises partway through the user has already had
    the subscription persisted (the caller does that before invoking us),
    so we just tell them the backfill was interrupted and return whatever
    count we got to. We do NOT roll the subscription back — the user
    explicitly asked to watch this keyword.
    """
    settings = _settings(context)
    client = _client(context)

    async with _locked_db(context) as conn:
        user_settings = get_user_settings(conn, user_id=user_id)
    if user_settings is None:
        return 0

    if update.message is not None:
        await update.message.reply_text(f"Backfill for {keyword!r} running (may take ~30-60s)...")

    sent = 0
    try:
        crawl_result = await scraper.crawl(
            client=client,
            district_ids=sorted(user_settings.districts),
            sleep_seconds=settings.scrape_sleep_seconds,
            keyword=keyword,
        )
    except Exception:
        logger.exception("backfill failed for user=%s keyword=%r", user_id, keyword)
        if update.message is not None:
            await update.message.reply_text(
                f"Backfill interrupted after {sent} matches; the keyword is saved."
            )
        return sent

    for snap in crawl_result.snapshots:
        if sent >= BACKFILL_CAP:
            break
        if snap.availability not in BOOKABLE_AVAILABILITY:
            continue
        matched_keywords = matches(snap, [keyword])
        if not matched_keywords:
            continue
        detail_url = settings.detail_url_template.format(kurs_id=snap.kurs_id)
        text, markup = course_card(snap, matched_keywords, "backfill", detail_url)
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=markup,
        )
        async with _locked_db(context) as conn:
            record_notification(conn, user_id=user_id, kurs_id=snap.kurs_id, reason="backfill")
        sent += 1

    if update.message is not None:
        # The truncation note exists because the live "/watch goldschmiede"
        # bug surfaced "0 matches sent" without any hint that the crawl
        # had only seen the first N pages of a deep district. Surfacing the
        # gap to the user lets them decide whether to retry, narrow their
        # districts, or accept the partial result.
        completion = f"Backfill done. {sent} matches sent."
        if crawl_result.truncated:
            completion += _TRUNCATION_NOTE
        await update.message.reply_text(completion)
    return sent


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    assert user is not None  # filter guarantees this

    async with _locked_db(context) as conn:
        existing = get_user_settings(conn, user_id=user.id)
        subs = list_subscriptions(conn, user_id=user.id) if existing is not None else []

    if existing is not None:
        text = build_list_text(
            keywords=subs,
            districts=sorted(existing.districts),
            paused=existing.paused,
        )
        greeting = escape_markdown_v2("Hi again! Here is your current setup:")
        assert update.message is not None
        await update.message.reply_text(
            f"{greeting}\n\n{text}",
            parse_mode="MarkdownV2",
        )
        return ConversationHandler.END

    # New user: kick off the onboarding conversation.
    fetched = await _safe_fetch_district_data(update, context)
    if fetched is None:
        return ConversationHandler.END
    district_map, district_names = fetched
    context.user_data[_UD_DISTRICT_MAP] = district_map
    context.user_data[_UD_DISTRICT_NAMES] = district_names
    context.user_data[_UD_SELECTED_DISTRICTS] = set()

    assert update.message is not None
    await update.message.reply_text(
        "Welcome! Pick the districts you want me to watch. Tap to toggle, then press Done.",
        reply_markup=build_district_keyboard(district_map, district_names, selected=set()),
    )
    return STATE_PICK_DISTRICTS


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(build_help_text(), parse_mode="MarkdownV2")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    async with _locked_db(context) as conn:
        settings = get_user_settings(conn, user_id=user.id)
        if settings is None:
            assert update.message is not None
            await update.message.reply_text(
                "You have not finished onboarding yet. Send /start first."
            )
            return
        subs = list_subscriptions(conn, user_id=user.id)
    text = build_list_text(
        keywords=subs,
        districts=sorted(settings.districts),
        paused=settings.paused,
    )
    assert update.message is not None
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    assert update.message is not None
    if not context.args:
        await update.message.reply_text("Usage: /watch <keyword>")
        return
    keyword = " ".join(context.args).strip()
    if not keyword:
        await update.message.reply_text("Usage: /watch <keyword>")
        return

    async with _locked_db(context) as conn:
        if get_user_settings(conn, user_id=user.id) is None:
            await update.message.reply_text(
                "You have not finished onboarding yet. Send /start first."
            )
            return
        add_subscription(conn, user_id=user.id, keyword=keyword)
    await update.message.reply_text(f"Watching {keyword!r}.")
    await _run_backfill(update=update, context=context, user_id=user.id, keyword=keyword)


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    assert update.message is not None
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <keyword>")
        return
    keyword = " ".join(context.args).strip()
    async with _locked_db(context) as conn:
        removed = remove_subscription(conn, user_id=user.id, keyword=keyword)
    if removed:
        await update.message.reply_text(f"Stopped watching {keyword!r}.")
    else:
        await update.message.reply_text(f"Was not watching {keyword!r}.")


async def districts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fetched = await _safe_fetch_district_data(update, context)
    if fetched is None:
        return ConversationHandler.END
    district_map, district_names = fetched
    context.user_data[_UD_DISTRICT_MAP] = district_map
    context.user_data[_UD_DISTRICT_NAMES] = district_names
    # Pre-seed selection with the user's current districts.
    user = update.effective_user
    assert user is not None
    async with _locked_db(context) as conn:
        existing = get_user_settings(conn, user_id=user.id)
    selected = set(existing.districts) if existing else set()
    context.user_data[_UD_SELECTED_DISTRICTS] = selected

    assert update.message is not None
    await update.message.reply_text(
        "Pick your districts. Tap to toggle, then press Done.",
        reply_markup=build_district_keyboard(district_map, district_names, selected=selected),
    )
    return STATE_PICK_DISTRICTS


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    async with _locked_db(context) as conn:
        if get_user_settings(conn, user_id=user.id) is None:
            assert update.message is not None
            await update.message.reply_text("Run /start first.")
            return
        set_paused(conn, user_id=user.id, paused=True)
    assert update.message is not None
    await update.message.reply_text("Notifications paused. Use /resume to un-pause.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    async with _locked_db(context) as conn:
        if get_user_settings(conn, user_id=user.id) is None:
            assert update.message is not None
            await update.message.reply_text("Run /start first.")
            return
        set_paused(conn, user_id=user.id, paused=False)
    assert update.message is not None
    await update.message.reply_text("Notifications resumed.")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual ``/scan`` trigger — wired to :func:`vhsbot.jobs.daily_scan`.

    Concurrency guard: ``application.bot_data[BD_SCAN_RUNNING]`` is a
    single bool flag. The scheduled ``daily_scan`` job sets the same flag
    on entry and resets it in its own finally (Phase 9), so a manual
    ``/scan`` issued during the scheduled window correctly defers
    rather than running two scans in parallel. The ``finally`` clears
    the flag even when ``daily_scan`` raises, so a transient crash
    does not strand the bot in a ``scan_running=True`` state.

    Tests patch ``vhsbot.handlers._daily_scan`` rather than
    ``vhsbot.jobs.daily_scan`` — the handler keeps a local module-level
    alias so the patch site is stable.
    """
    user = update.effective_user
    assert user is not None
    assert update.message is not None
    logger.info("manual /scan requested by user_id=%s", user.id)

    if context.bot_data.get(BD_SCAN_RUNNING):
        await update.message.reply_text("A scan is already running, try again in a few minutes.")
        return

    context.bot_data[BD_SCAN_RUNNING] = True
    try:
        await update.message.reply_text("Manual scan started.")
        # Call through the module-level alias so tests can monkeypatch
        # ``handlers._daily_scan`` directly.
        await _daily_scan(context)
        await update.message.reply_text("Manual scan complete.")
    finally:
        context.bot_data[BD_SCAN_RUNNING] = False


# ---------------------------------------------------------------------------
# Onboarding conversation callbacks
# ---------------------------------------------------------------------------


async def on_district_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle a tap on the district keyboard."""
    query = update.callback_query
    assert query is not None

    district_map: dict[int, int] = context.user_data.get(_UD_DISTRICT_MAP, {})
    district_names: dict[int, str] = context.user_data.get(_UD_DISTRICT_NAMES, {})
    selected: set[int] = context.user_data.get(_UD_SELECTED_DISTRICTS, set())

    data = query.data or ""
    if data == "all":
        await query.answer()
        selected = set(district_map.keys())
    elif data == "done":
        if not selected:
            # Single answer per branch: the alert IS the answer.
            await query.answer("Pick at least one district first.", show_alert=True)
            return STATE_PICK_DISTRICTS
        await query.answer()
        user = update.effective_user
        assert user is not None
        async with _locked_db(context) as conn:
            upsert_user_settings(conn, user_id=user.id, districts=selected)
        await query.edit_message_text(
            f"Districts saved: {sorted(selected)}. Now send me a keyword to watch (e.g. 'Yoga')."
        )
        return STATE_PICK_KEYWORD
    elif data.startswith("toggle:"):
        await query.answer()
        try:
            district_id = int(data.split(":", 1)[1])
        except ValueError:
            return STATE_PICK_DISTRICTS
        if district_id in selected:
            selected.discard(district_id)
        else:
            selected.add(district_id)
    else:
        await query.answer()

    context.user_data[_UD_SELECTED_DISTRICTS] = selected
    await query.edit_message_reply_markup(
        reply_markup=build_district_keyboard(district_map, district_names, selected=selected)
    )
    return STATE_PICK_DISTRICTS


async def on_keyword_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the first-keyword subscription and run a backfill."""
    user = update.effective_user
    assert user is not None
    assert update.message is not None
    keyword = (update.message.text or "").strip()
    if not keyword:
        await update.message.reply_text("Please send a non-empty keyword.")
        return STATE_PICK_KEYWORD

    async with _locked_db(context) as conn:
        add_subscription(conn, user_id=user.id, keyword=keyword)
    await update.message.reply_text(f"Watching {keyword!r}.")
    await _run_backfill(update=update, context=context, user_id=user.id, keyword=keyword)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/cancel`` handler — explicit user exit from the conversation.

    Wipes the transient onboarding state (district map + selection) so a
    later ``/start`` begins fresh.
    """
    context.user_data.pop(_UD_DISTRICT_MAP, None)
    context.user_data.pop(_UD_DISTRICT_NAMES, None)
    context.user_data.pop(_UD_SELECTED_DISTRICTS, None)
    if update.message is not None:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def conversation_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Catch-all slash-command fallback inside the keyword state.

    If the user sends ANY slash command while we're waiting for a
    keyword, exit the conversation cleanly with a clear message. Their
    saved districts (if persisted at the Done step) remain — only the
    pending-keyword state is dropped.
    """
    context.user_data.pop(_UD_DISTRICT_MAP, None)
    context.user_data.pop(_UD_DISTRICT_NAMES, None)
    context.user_data.pop(_UD_SELECTED_DISTRICTS, None)
    if update.message is not None:
        await update.message.reply_text(
            "Onboarding cancelled. Use the command again or /start fresh."
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def register_handlers(application: Application) -> None:
    """Attach every handler to ``application``. Called from main.py.

    Access control is structural: the ``filters.User`` instance below is
    the whitelist. Non-whitelisted updates are silently ignored by PTB —
    the bot looks "off" to outsiders, which is the desired stance.
    """
    settings: Settings = application.bot_data[BD_SETTINGS]
    whitelist = filters.User(user_id=list(settings.allowed_user_ids))

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start, filters=whitelist)],
        states={
            STATE_PICK_DISTRICTS: [
                CallbackQueryHandler(
                    _whitelist_callback(on_district_toggle),
                    pattern=r"^(toggle:\d+|all|done)$",
                )
            ],
            STATE_PICK_KEYWORD: [
                MessageHandler(
                    whitelist & filters.TEXT & ~filters.COMMAND,
                    on_keyword_message,
                ),
                # Any slash command while we're waiting for a keyword
                # cleanly aborts the conversation.
                MessageHandler(
                    whitelist & filters.COMMAND,
                    conversation_interrupt,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=whitelist),
            CommandHandler(
                ["help", "list", "watch", "unwatch", "districts", "pause", "resume", "scan"],
                conversation_interrupt,
                filters=whitelist,
            ),
        ],
        per_message=False,
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_cmd, filters=whitelist))
    application.add_handler(CommandHandler("list", list_cmd, filters=whitelist))
    application.add_handler(CommandHandler("watch", watch, filters=whitelist))
    application.add_handler(CommandHandler("unwatch", unwatch, filters=whitelist))
    application.add_handler(CommandHandler("districts", districts_cmd, filters=whitelist))
    application.add_handler(CommandHandler("pause", pause, filters=whitelist))
    application.add_handler(CommandHandler("resume", resume, filters=whitelist))
    application.add_handler(CommandHandler("scan", scan, filters=whitelist))
    application.add_error_handler(global_error_handler)
