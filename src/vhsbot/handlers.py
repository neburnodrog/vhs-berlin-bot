"""Telegram handlers, whitelist middleware, and onboarding conversation.

Every handler runs the whitelist check first (see :func:`whitelist_only`)
and wraps its body in a try/except that logs with ``logger.exception``
and surfaces a generic apology to the user — Telegram never sees a stack
trace.

The on-demand backfill on ``/watch`` blocks the handler (option A from
the Phase 4 spec): we await ``scraper.crawl`` for the user's districts
and send at most 15 bookable matches as individual messages, one per
course. The user sees a typing indicator for up to a minute. This is
fine for a single-user bot. See ``main.py`` docstring for the rationale
and Phase 5 deferral notes.
"""

from __future__ import annotations

import logging
import sqlite3
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

from vhsbot import scraper
from vhsbot.config import Settings
from vhsbot.db import (
    BOOKABLE_AVAILABILITY,
    CourseSnapshot,
    add_subscription,
    all_active_user_ids,  # noqa: F401  (re-exported for Phase 5)
    get_user_settings,
    list_subscriptions,
    record_notification,
    remove_subscription,
    set_paused,
    upsert_user_settings,
)
from vhsbot.matching import matches
from vhsbot.parser import parse_district_map

logger = logging.getLogger(__name__)

# Conversation states
STATE_PICK_DISTRICTS = 1
STATE_PICK_KEYWORD = 2

# Per-handler cap on the on-demand backfill. The cross-day 15-msg cap is
# deferred to Phase 5 (the daily-scan code path); see tasks/todo .md.
BACKFILL_CAP = 15

# bot_data keys
_BD_SETTINGS = "settings"
_BD_DB = "db"
_BD_CLIENT = "http_client"

# user_data keys (per-user state inside the onboarding ConversationHandler)
_UD_DISTRICT_MAP = "district_map"  # dict[int, int]
_UD_SELECTED_DISTRICTS = "selected_districts"  # set[int]


# ---------------------------------------------------------------------------
# Pure helpers (testable without Telegram or DB)
# ---------------------------------------------------------------------------


_MD2_RESERVED = r"_*[]()~`>#+-=|{}.!\\"


def escape_markdown_v2(s: str) -> str:
    """Escape every Telegram Markdown V2 reserved character in ``s``.

    Reserved set (from the Bot API docs):
    ``_ * [ ] ( ) ~ backtick > # + - = | { } . !`` plus backslash.
    Each occurrence is prefixed with ``\\``.
    """
    out: list[str] = []
    for ch in s:
        if ch in _MD2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


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
        "/scan — trigger a manual scan (admin only)",
    ]
    return escape_markdown_v2("\n".join(lines))


def build_list_text(keywords: list[str], districts: list[int], paused: bool) -> str:
    """Markdown V2 formatted summary of the user's subscription state."""
    kw_block = ", ".join(keywords) if keywords else "(none yet — use /watch)"
    dist_block = ", ".join(str(d) for d in sorted(districts)) if districts else "(none)"
    status = "PAUSED" if paused else "active"
    plain = f"Status: {status}\nKeywords: {kw_block}\nDistricts: {dist_block}"
    return escape_markdown_v2(plain)


def build_course_message(
    course: CourseSnapshot,
    matched_keywords: list[str],
    detail_url: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the notification text + inline button for a single course."""
    title = escape_markdown_v2(course.title)
    cnum = escape_markdown_v2(course.course_number)
    district = escape_markdown_v2(course.district or "")
    date_range = escape_markdown_v2(course.date_range or "")
    avail = escape_markdown_v2(course.availability)
    matched = escape_markdown_v2(", ".join(matched_keywords))

    parts = [
        f"*{title}*",
        f"Kurs: `{cnum}`",
    ]
    if district:
        parts.append(f"Bezirk: {district}")
    if date_range:
        parts.append(f"Termin: {date_range}")
    parts.append(f"Plätze: {avail}")
    if matched_keywords:
        parts.append(f"Matched: {matched}")

    text = "\n".join(parts)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Details öffnen", url=detail_url)]])
    return text, markup


def build_district_keyboard(
    district_map: dict[int, int], selected: set[int]
) -> InlineKeyboardMarkup:
    """Render the multi-select district keyboard.

    Layout: 3 columns of district toggles in ascending district-id order,
    plus a final row with "Alle" (select all) and "Fertig" (confirm).
    Selected districts show a leading ``[x]`` marker — no emojis per the
    Phase 4 spec.
    """
    sorted_ids = sorted(district_map.keys())
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for district_id in sorted_ids:
        marker = "[x] " if district_id in selected else ""
        label = f"{marker}{district_id}"
        current_row.append(InlineKeyboardButton(label, callback_data=f"toggle:{district_id}"))
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [
            InlineKeyboardButton("Alle", callback_data="all"),
            InlineKeyboardButton("Fertig", callback_data="done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Whitelist middleware
# ---------------------------------------------------------------------------


_HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def whitelist_only(fn: _HandlerFn) -> _HandlerFn:
    """Reject updates whose effective_user.id is not whitelisted.

    The decorator answers with a polite rejection message and returns
    ``ConversationHandler.END`` so it is also safe as an entry point of
    the onboarding conversation. Every handler in this module is wrapped
    with this decorator AND has an outer try/except that maps any
    exception to a generic apology.
    """

    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        settings: Settings = context.bot_data[_BD_SETTINGS]
        user = update.effective_user
        if user is None or user.id not in settings.allowed_user_ids:
            logger.info(
                "rejecting update from non-whitelisted user_id=%s",
                user.id if user else None,
            )
            if update.message is not None:
                await update.message.reply_text(
                    "Sorry, this is a private bot. Access is restricted."
                )
            return ConversationHandler.END
        try:
            return await fn(update, context)
        except Exception:
            logger.exception("handler %s raised", fn.__name__)
            if update.message is not None:
                try:
                    await update.message.reply_text(
                        "Something went wrong on my side. Please try again."
                    )
                except Exception:  # pragma: no cover  (best-effort apology)
                    logger.exception("failed to send fallback apology message")
            return ConversationHandler.END

    return wrapper


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data[_BD_SETTINGS]


def _conn(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data[_BD_DB]


def _client(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    return context.bot_data[_BD_CLIENT]


async def _fetch_district_map(client: httpx.AsyncClient, settings: Settings) -> dict[int, int]:
    """Fetch the search form and parse its district checkbox map.

    Pulled out as a thin helper so tests can monkeypatch it without
    touching the network.
    """
    resp = await client.get(settings.search_url)
    return parse_district_map(resp.content)


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
    """
    settings = _settings(context)
    conn = _conn(context)
    client = _client(context)

    user_settings = get_user_settings(conn, user_id=user_id)
    if user_settings is None:
        return 0

    if update.message is not None:
        await update.message.reply_text(f"Backfill für {keyword!r} läuft (kann ~30-60s dauern)...")

    snapshots = await scraper.crawl(
        client=client,
        district_ids=sorted(user_settings.districts),
        sleep_seconds=settings.scrape_sleep_seconds,
    )

    sent = 0
    for snap in snapshots:
        if sent >= BACKFILL_CAP:
            break
        if snap.availability not in BOOKABLE_AVAILABILITY:
            continue
        matched_keywords = matches(snap, [keyword])
        if not matched_keywords:
            continue
        detail_url = settings.detail_url_template.format(kurs_id=snap.kurs_id)
        text, markup = build_course_message(snap, matched_keywords, detail_url)
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=markup,
        )
        record_notification(conn, user_id=user_id, kurs_id=snap.kurs_id, reason="backfill")
        sent += 1

    if update.message is not None:
        await update.message.reply_text(f"Backfill fertig. {sent} Treffer gesendet.")
    return sent


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@whitelist_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    assert user is not None  # whitelist_only guarantees this
    conn = _conn(context)

    existing = get_user_settings(conn, user_id=user.id)
    if existing is not None:
        subs = list_subscriptions(conn, user_id=user.id)
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
    client = _client(context)
    settings = _settings(context)
    district_map = await _fetch_district_map(client, settings)
    context.user_data[_UD_DISTRICT_MAP] = district_map
    context.user_data[_UD_SELECTED_DISTRICTS] = set()

    assert update.message is not None
    await update.message.reply_text(
        "Welcome! Pick the districts you want me to watch. Tap to toggle, then press Fertig.",
        reply_markup=build_district_keyboard(district_map, selected=set()),
    )
    return STATE_PICK_DISTRICTS


@whitelist_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(build_help_text(), parse_mode="MarkdownV2")


@whitelist_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    conn = _conn(context)
    settings = get_user_settings(conn, user_id=user.id)
    if settings is None:
        assert update.message is not None
        await update.message.reply_text("You have not finished onboarding yet. Send /start first.")
        return
    subs = list_subscriptions(conn, user_id=user.id)
    text = build_list_text(
        keywords=subs,
        districts=sorted(settings.districts),
        paused=settings.paused,
    )
    assert update.message is not None
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@whitelist_only
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

    conn = _conn(context)
    if get_user_settings(conn, user_id=user.id) is None:
        await update.message.reply_text("You have not finished onboarding yet. Send /start first.")
        return

    add_subscription(conn, user_id=user.id, keyword=keyword)
    await update.message.reply_text(f"Watching {keyword!r}.")
    await _run_backfill(update=update, context=context, user_id=user.id, keyword=keyword)


@whitelist_only
async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    assert update.message is not None
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <keyword>")
        return
    keyword = " ".join(context.args).strip()
    conn = _conn(context)
    removed = remove_subscription(conn, user_id=user.id, keyword=keyword)
    if removed:
        await update.message.reply_text(f"Stopped watching {keyword!r}.")
    else:
        await update.message.reply_text(f"Was not watching {keyword!r}.")


@whitelist_only
async def districts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client = _client(context)
    settings = _settings(context)
    district_map = await _fetch_district_map(client, settings)
    context.user_data[_UD_DISTRICT_MAP] = district_map
    # Pre-seed selection with the user's current districts.
    user = update.effective_user
    assert user is not None
    conn = _conn(context)
    existing = get_user_settings(conn, user_id=user.id)
    selected = set(existing.districts) if existing else set()
    context.user_data[_UD_SELECTED_DISTRICTS] = selected

    assert update.message is not None
    await update.message.reply_text(
        "Pick your districts. Tap to toggle, then press Fertig.",
        reply_markup=build_district_keyboard(district_map, selected=selected),
    )
    return STATE_PICK_DISTRICTS


@whitelist_only
async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    conn = _conn(context)
    if get_user_settings(conn, user_id=user.id) is None:
        assert update.message is not None
        await update.message.reply_text("Run /start first.")
        return
    set_paused(conn, user_id=user.id, paused=True)
    assert update.message is not None
    await update.message.reply_text("Notifications paused. Use /resume to un-pause.")


@whitelist_only
async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user is not None
    conn = _conn(context)
    if get_user_settings(conn, user_id=user.id) is None:
        assert update.message is not None
        await update.message.reply_text("Run /start first.")
        return
    set_paused(conn, user_id=user.id, paused=False)
    assert update.message is not None
    await update.message.reply_text("Notifications resumed.")


@whitelist_only
async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # TODO(Phase 5): wire this to jobs.daily_scan(context) — currently the
    # daily-scan job does not exist yet, so /scan is a stubbed trigger that
    # only logs and acknowledges the user.
    logger.info("manual /scan requested by user_id=%s", update.effective_user.id)  # type: ignore[union-attr]
    assert update.message is not None
    await update.message.reply_text("Manual scan triggered. (Daily-scan wiring lands in Phase 5.)")


# ---------------------------------------------------------------------------
# Onboarding conversation callbacks
# ---------------------------------------------------------------------------


async def on_district_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle a tap on the district keyboard."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    district_map: dict[int, int] = context.user_data.get(_UD_DISTRICT_MAP, {})
    selected: set[int] = context.user_data.get(_UD_SELECTED_DISTRICTS, set())

    data = query.data or ""
    if data == "all":
        selected = set(district_map.keys())
    elif data == "done":
        if not selected:
            await query.answer("Pick at least one district first.", show_alert=True)
            return STATE_PICK_DISTRICTS
        user = update.effective_user
        assert user is not None
        conn = _conn(context)
        upsert_user_settings(conn, user_id=user.id, districts=selected)
        await query.edit_message_text(
            f"Districts saved: {sorted(selected)}. Now send me a keyword to watch (e.g. 'Yoga')."
        )
        return STATE_PICK_KEYWORD
    elif data.startswith("toggle:"):
        try:
            district_id = int(data.split(":", 1)[1])
        except ValueError:
            return STATE_PICK_DISTRICTS
        if district_id in selected:
            selected.discard(district_id)
        else:
            selected.add(district_id)
    context.user_data[_UD_SELECTED_DISTRICTS] = selected
    await query.edit_message_reply_markup(
        reply_markup=build_district_keyboard(district_map, selected=selected)
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

    conn = _conn(context)
    add_subscription(conn, user_id=user.id, keyword=keyword)
    await update.message.reply_text(f"Watching {keyword!r}.")
    await _run_backfill(update=update, context=context, user_id=user.id, keyword=keyword)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is not None:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def register_handlers(application: Application) -> None:
    """Attach every handler to ``application``. Called from main.py."""
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_PICK_DISTRICTS: [
                CallbackQueryHandler(on_district_toggle, pattern=r"^(toggle:\d+|all|done)$")
            ],
            STATE_PICK_KEYWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_keyword_message)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("unwatch", unwatch))
    application.add_handler(CommandHandler("districts", districts_cmd))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))
    application.add_handler(CommandHandler("scan", scan))
