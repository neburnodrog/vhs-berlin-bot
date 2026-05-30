"""Tests for the Telegram handlers layer (Phase 4).

Three groups:

1. Pure-function tests for the message builders and the Markdown V2
   escaper. These run without any Telegram or DB I/O and form the bulk
   of the coverage.
2. Handler smoke tests that build fake ``Update`` / ``Context`` objects
   with ``unittest.mock.AsyncMock`` and assert on the bot method calls.
3. Wiring tests that build an actual ``Application`` and assert the
   ``filters.User`` structural whitelist is wired into every command
   handler — replaces the per-handler decorator that earlier reviews
   flagged as fragile.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
)

from vhsbot import db, handlers
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.db import CourseSnapshot

# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestEscapeMarkdownV2:
    def test_escapes_every_reserved_character(self) -> None:
        raw = r"_*[]()~`>#+-=|{}.!"
        out = handlers.escape_markdown_v2(raw)
        # Every char in the reserved set should now be prefixed by a backslash.
        for ch in raw:
            assert f"\\{ch}" in out
        # And the output length should be exactly 2x the input length.
        assert len(out) == 2 * len(raw)

    def test_does_not_escape_plain_letters_and_digits(self) -> None:
        assert handlers.escape_markdown_v2("Hallo Welt 123") == "Hallo Welt 123"

    def test_backslash_is_escaped(self) -> None:
        # PTB's escape_markdown(version=2) escapes backslash too — Telegram
        # would otherwise treat ``\X`` as a deliberate escape of ``X``, so
        # raw backslashes inside user input must be doubled.
        assert handlers.escape_markdown_v2("a\\b") == "a\\\\b"

    def test_idempotent_on_empty_string(self) -> None:
        assert handlers.escape_markdown_v2("") == ""

    def test_periods_and_dashes_in_dates(self) -> None:
        # Real workload: a date range must come out with escaped periods.
        assert handlers.escape_markdown_v2("01.06.2026") == "01\\.06\\.2026"


class TestBuildHelpText:
    def test_lists_every_command(self) -> None:
        text = handlers.build_help_text()
        for cmd in (
            "/start",
            "/help",
            "/list",
            "/watch",
            "/unwatch",
            "/districts",
            "/pause",
            "/resume",
            "/scan",
        ):
            assert cmd in text

    def test_does_not_claim_scan_is_admin_only(self) -> None:
        # MINOR fix: no admin gate exists yet (deferred to Phase 5),
        # so help must not claim there is one.
        text = handlers.build_help_text()
        assert "admin" not in text.lower()


class TestBuildListText:
    def test_renders_keywords_and_districts(self) -> None:
        text = handlers.build_list_text(
            keywords=["Yoga", "Tango"],
            districts=[31, 39],
            paused=False,
        )
        assert "Yoga" in text
        assert "Tango" in text
        assert "31" in text
        assert "39" in text

    def test_paused_branch_announces_paused_state(self) -> None:
        text = handlers.build_list_text(keywords=["Yoga"], districts=[31], paused=True)
        assert "paused" in text.lower()

    def test_active_branch_does_not_say_paused(self) -> None:
        text = handlers.build_list_text(keywords=["Yoga"], districts=[31], paused=False)
        assert "paused" not in text.lower()

    def test_empty_keywords_handled_gracefully(self) -> None:
        text = handlers.build_list_text(keywords=[], districts=[31], paused=False)
        # Should not crash and should hint at no subscriptions.
        assert isinstance(text, str)
        assert text  # non-empty

    def test_markdown_v2_special_chars_in_keyword_are_escaped(self) -> None:
        # Period and dash are both MD-V2 specials.
        text = handlers.build_list_text(keywords=["yoga.v2-soft"], districts=[31], paused=False)
        assert "\\." in text
        assert "\\-" in text


class TestBuildDistrictKeyboard:
    def test_shows_checkmark_marker_on_selected(self) -> None:
        district_map = {31: 5, 39: 6, 38: 7}
        markup = handlers.build_district_keyboard(district_map, selected={31})
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        # Selected districts get a "[x]" marker; unselected do not.
        selected_label = next(label for label in labels if "31" in label)
        unselected_label = next(label for label in labels if "39" in label)
        assert "[x]" in selected_label
        assert "[x]" not in unselected_label

    def test_includes_alle_and_fertig_buttons(self) -> None:
        markup = handlers.build_district_keyboard({31: 5}, selected=set())
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        callbacks = {btn.callback_data for btn in all_buttons}
        assert "all" in callbacks
        assert "done" in callbacks

    def test_per_district_callback_data_is_toggle_id(self) -> None:
        markup = handlers.build_district_keyboard({31: 5, 39: 6}, selected=set())
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        callbacks = {btn.callback_data for btn in all_buttons}
        assert "toggle:31" in callbacks
        assert "toggle:39" in callbacks

    def test_uses_three_columns_per_row_for_district_buttons(self) -> None:
        # 7 districts -> 3 rows of 3 + 1 of 1 (or 3/3/1) for the district grid.
        # We don't pin the exact split, but we do pin "no row exceeds 3 columns"
        # for the district-row portion.
        district_map = dict.fromkeys(range(31, 38), 0)
        markup = handlers.build_district_keyboard(district_map, selected=set())
        # All rows except the final Alle/Fertig row must have at most 3 buttons.
        district_rows = markup.inline_keyboard[:-1]
        for row in district_rows:
            assert len(row) <= 3


# ---------------------------------------------------------------------------
# Handler smoke tests (with mocks)
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="testtoken",
        allowed_user_ids=frozenset({111}),
        scan_time=time(hour=8),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=Path("/tmp/vhsbot-test.db"),
        snapshot_dir=Path("/tmp/vhsbot-test-snap"),
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _make_update(user_id: int, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.from_user.id = user_id
    return update


class _AsyncContextLock:
    """Real-enough async lock that records ``__aenter__`` calls.

    We can't use an ``asyncio.Lock`` directly without an event loop in
    sync test fixtures, and ``AsyncMock`` swallows ``async with`` —
    so we hand-roll a tiny one whose entry count we can assert on.
    """

    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _AsyncContextLock:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exit_count += 1


def _make_context(
    *,
    settings: Settings,
    conn: sqlite3.Connection,
    client: object,
    args: list[str] | None = None,
    db_lock: _AsyncContextLock | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot_data = {
        BD_SETTINGS: settings,
        BD_DB: conn,
        BD_DB_LOCK: db_lock or _AsyncContextLock(),
        BD_CLIENT: client,
    }
    ctx.args = args or []
    ctx.user_data = {}
    return ctx


class TestStartHandler:
    async def test_whitelisted_new_user_kicks_off_conversation(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the initial-form GET so start() can render the district keyboard
        # without real HTTP. We monkeypatch _fetch_district_map to return a tiny map.
        monkeypatch.setattr(handlers, "_fetch_district_map", AsyncMock(return_value={31: 5, 39: 6}))
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object())

        state = await handlers.start(update, ctx)

        update.message.reply_text.assert_awaited()
        # Should have been called with an inline keyboard kwarg.
        kwargs = update.message.reply_text.await_args.kwargs
        assert "reply_markup" in kwargs
        assert state == handlers.STATE_PICK_DISTRICTS

    async def test_whitelisted_known_user_gets_greeting_with_subs(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        db.add_subscription(conn, user_id=111, keyword="Yoga")
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object())

        await handlers.start(update, ctx)

        update.message.reply_text.assert_awaited()
        text = update.message.reply_text.await_args.args[0]
        assert "Yoga" in text

    async def test_start_acquires_db_lock(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        # BLOCKER-1 regression pin: every DB-touching handler must run under
        # the shared asyncio.Lock.
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        lock = _AsyncContextLock()
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), db_lock=lock)

        await handlers.start(update, ctx)

        assert lock.enter_count >= 1
        assert lock.exit_count == lock.enter_count

    async def test_start_handles_district_map_fetch_failure(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MINOR: _fetch_district_map raising httpx.HTTPError should produce
        # a friendly "site down" message instead of a generic apology.
        async def boom(*a: object, **kw: object) -> dict[int, int]:
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(handlers, "_fetch_district_map", boom)
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object())

        state = await handlers.start(update, ctx)

        from telegram.ext import ConversationHandler

        assert state == ConversationHandler.END
        update.message.reply_text.assert_awaited()
        text = update.message.reply_text.await_args.args[0]
        assert "down" in text.lower() or "moment" in text.lower()


class TestWatchHandler:
    async def test_user_without_settings_is_redirected_to_start(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])
        await handlers.watch(update, ctx)
        text = update.message.reply_text.await_args.args[0]
        assert "/start" in text
        # Did NOT insert a subscription.
        assert db.list_subscriptions(conn, user_id=111) == []

    async def test_whitelisted_user_inserts_subscription_and_runs_backfill(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])

        async def fake_crawl(
            *, client: object, district_ids: Iterable[int], sleep_seconds: float
        ) -> list[CourseSnapshot]:
            return [
                CourseSnapshot(
                    kurs_id=1,
                    title="Yoga sanft",
                    course_number="Mi251-001K",
                    district="Mitte",
                    venue=None,
                    date_range="01.06.2026",
                    availability=">2",
                ),
            ]

        monkeypatch.setattr(handlers.scraper, "crawl", fake_crawl)

        await handlers.watch(update, ctx)

        assert db.list_subscriptions(conn, user_id=111) == ["Yoga"]
        # Sent the backfill notification.
        assert ctx.bot.send_message.await_count == 1

    async def test_backfill_respects_15_message_cap(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])

        async def fake_crawl_many(
            *, client: object, district_ids: Iterable[int], sleep_seconds: float
        ) -> list[CourseSnapshot]:
            return [
                CourseSnapshot(
                    kurs_id=i,
                    title=f"Yoga {i}",
                    course_number=f"Mi251-{i:03d}K",
                    district="Mitte",
                    venue=None,
                    date_range="01.06.2026",
                    availability=">2",
                )
                for i in range(30)
            ]

        monkeypatch.setattr(handlers.scraper, "crawl", fake_crawl_many)

        await handlers.watch(update, ctx)

        # 30 matches available, but the cap is 15.
        assert ctx.bot.send_message.await_count == 15

    @pytest.mark.parametrize(
        "total,expected",
        [
            (14, 14),  # under the cap, send all
            (15, 15),  # exactly the cap
            (16, 15),  # one over, still 15
        ],
    )
    async def test_backfill_cap_boundaries(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
        total: int,
        expected: int,
    ) -> None:
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])

        async def fake_crawl(
            *, client: object, district_ids: Iterable[int], sleep_seconds: float
        ) -> list[CourseSnapshot]:
            return [
                CourseSnapshot(
                    kurs_id=i,
                    title=f"Yoga {i}",
                    course_number=f"Mi251-{i:03d}K",
                    district="Mitte",
                    venue=None,
                    date_range="01.06.2026",
                    availability=">2",
                )
                for i in range(total)
            ]

        monkeypatch.setattr(handlers.scraper, "crawl", fake_crawl)
        await handlers.watch(update, ctx)
        assert ctx.bot.send_message.await_count == expected

    async def test_backfill_skips_non_bookable_snapshots(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # MINOR: non-bookable courses must be filtered out of the backfill.
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])

        # 30 snapshots, half bookable, half "belegt". Expected: 15 sent (the
        # bookable half).
        availabilities = [">2", "belegt"] * 15

        async def fake_crawl(
            *, client: object, district_ids: Iterable[int], sleep_seconds: float
        ) -> list[CourseSnapshot]:
            return [
                CourseSnapshot(
                    kurs_id=i,
                    title=f"Yoga {i}",
                    course_number=f"Mi251-{i:03d}K",
                    district="Mitte",
                    venue=None,
                    date_range="01.06.2026",
                    availability=availabilities[i],
                )
                for i in range(30)
            ]

        monkeypatch.setattr(handlers.scraper, "crawl", fake_crawl)
        await handlers.watch(update, ctx)
        # 15 bookable in the input → 15 sent (the cap is also 15, fine).
        assert ctx.bot.send_message.await_count == 15

    async def test_backfill_crawl_exception_does_not_strand_user(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # MAJOR-4 regression pin: if scraper.crawl raises, the user must
        # see a clear "interrupted" message AND their subscription must
        # still be present AND the handler must return cleanly (no raise).
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])

        async def boom(
            *, client: object, district_ids: Iterable[int], sleep_seconds: float
        ) -> list[CourseSnapshot]:
            raise RuntimeError("VHS Berlin returned 503")

        monkeypatch.setattr(handlers.scraper, "crawl", boom)

        # Must not raise.
        await handlers.watch(update, ctx)

        # Subscription is still there.
        assert db.list_subscriptions(conn, user_id=111) == ["Yoga"]
        # The user got an "interrupted" message somewhere in the reply chain.
        replies = [call.args[0] for call in update.message.reply_text.await_args_list]
        assert any("interrupted" in r.lower() for r in replies)
        # No backfill messages were actually sent.
        assert ctx.bot.send_message.await_count == 0


# ---------------------------------------------------------------------------
# Callback handler tests
# ---------------------------------------------------------------------------


class TestDistrictToggle:
    def _make_callback_update(self, user_id: int, callback_data: str) -> MagicMock:
        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.id = user_id
        update.callback_query = MagicMock()
        update.callback_query.data = callback_data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.message = None
        return update

    async def test_done_with_empty_selection_shows_alert_once(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        # MAJOR-7 regression pin: the empty-done branch must answer the
        # query exactly once (the alert IS the answer), not twice.
        update = self._make_callback_update(user_id=111, callback_data="done")
        ctx = _make_context(settings=settings, conn=conn, client=object())
        ctx.user_data[handlers._UD_DISTRICT_MAP] = {31: 5}
        ctx.user_data[handlers._UD_SELECTED_DISTRICTS] = set()

        state = await handlers.on_district_toggle(update, ctx)

        assert update.callback_query.answer.await_count == 1
        # And the single answer is the alert.
        call = update.callback_query.answer.await_args
        assert call.kwargs.get("show_alert") is True
        assert state == handlers.STATE_PICK_DISTRICTS

    async def test_toggle_branch_answers_once(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        update = self._make_callback_update(user_id=111, callback_data="toggle:31")
        ctx = _make_context(settings=settings, conn=conn, client=object())
        ctx.user_data[handlers._UD_DISTRICT_MAP] = {31: 5}
        ctx.user_data[handlers._UD_SELECTED_DISTRICTS] = set()

        await handlers.on_district_toggle(update, ctx)
        assert update.callback_query.answer.await_count == 1

    async def test_done_with_selection_saves_and_advances(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        update = self._make_callback_update(user_id=111, callback_data="done")
        ctx = _make_context(settings=settings, conn=conn, client=object())
        ctx.user_data[handlers._UD_DISTRICT_MAP] = {31: 5, 39: 6}
        ctx.user_data[handlers._UD_SELECTED_DISTRICTS] = {31}

        state = await handlers.on_district_toggle(update, ctx)

        assert state == handlers.STATE_PICK_KEYWORD
        # Settings row was upserted.
        saved = db.get_user_settings(conn, user_id=111)
        assert saved is not None
        assert 31 in saved.districts


class TestCancelAndConversationInterrupt:
    async def test_cancel_clears_user_data(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        # MAJOR-3 pin: /cancel must wipe transient onboarding state.
        update = _make_update(user_id=111)
        ctx = _make_context(settings=settings, conn=conn, client=object())
        ctx.user_data[handlers._UD_DISTRICT_MAP] = {31: 5}
        ctx.user_data[handlers._UD_SELECTED_DISTRICTS] = {31}

        state = await handlers.cancel(update, ctx)

        from telegram.ext import ConversationHandler

        assert state == ConversationHandler.END
        assert handlers._UD_DISTRICT_MAP not in ctx.user_data
        assert handlers._UD_SELECTED_DISTRICTS not in ctx.user_data

    async def test_slash_command_during_keyword_state_cancels_conversation(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        # MAJOR-3 pin: a slash command interleaved into STATE_PICK_KEYWORD
        # exits the conversation cleanly with a polite message, and the
        # transient state is wiped (the persisted districts remain).
        db.upsert_user_settings(conn, user_id=111, districts=[31])
        update = _make_update(user_id=111, text="/help")
        ctx = _make_context(settings=settings, conn=conn, client=object())
        ctx.user_data[handlers._UD_DISTRICT_MAP] = {31: 5}
        ctx.user_data[handlers._UD_SELECTED_DISTRICTS] = {31}

        state = await handlers.conversation_interrupt(update, ctx)

        from telegram.ext import ConversationHandler

        assert state == ConversationHandler.END
        # The polite message was sent.
        update.message.reply_text.assert_awaited()
        text = update.message.reply_text.await_args.args[0]
        assert "cancel" in text.lower() or "/start" in text.lower()
        # Persisted districts are still there.
        saved = db.get_user_settings(conn, user_id=111)
        assert saved is not None
        assert 31 in saved.districts


# ---------------------------------------------------------------------------
# Wiring / structural-whitelist tests
# ---------------------------------------------------------------------------


def _build_app(settings: Settings) -> Application:
    app = Application.builder().token("123:TEST").build()
    app.bot_data[BD_SETTINGS] = settings
    handlers.register_handlers(app)
    return app


def _whitelisted_user(user_id: int) -> User:
    return User(id=user_id, first_name="Test", is_bot=False)


class TestStructuralWhitelist:
    """Pin that filters.User is applied to every CommandHandler/MessageHandler.

    The fragile @whitelist_only decorator (3 callbacks missed in Phase 4)
    is replaced by a single filter applied at registration time. These
    tests assert the filter actually rejects non-whitelisted updates by
    introspecting the registered handlers and calling
    ``handler.check_update`` directly.
    """

    def test_every_command_handler_has_user_filter(self, settings: Settings) -> None:
        app = _build_app(settings)
        command_handlers: list[CommandHandler] = []
        for group in app.handlers.values():
            for h in group:
                if isinstance(h, CommandHandler):
                    command_handlers.append(h)
                elif isinstance(h, ConversationHandler):
                    # Recurse into entry_points and fallbacks.
                    for sub in (*h.entry_points, *h.fallbacks):
                        if isinstance(sub, CommandHandler):
                            command_handlers.append(sub)
        # All eight slash commands + /start in conv entry + the conv fallbacks
        # must be filter-protected.
        assert command_handlers, "no command handlers registered?"
        for h in command_handlers:
            # PTB stores the filter on .filters; if None, the handler is wide open.
            assert h.filters is not None, f"handler {h.commands} has no filter"

    @pytest.mark.parametrize(
        "command",
        ["start", "help", "list", "unwatch", "districts", "pause", "resume", "scan"],
    )
    def test_non_whitelisted_user_is_filtered_out_of_command_handlers(
        self, settings: Settings, command: str
    ) -> None:
        # MINOR: the seven missing whitelist-rejection tests. Use PTB's
        # actual filter to assert non-whitelisted updates are dropped.
        app = _build_app(settings)
        outsider = _whitelisted_user(user_id=999)  # not in {111}

        chat = Chat(id=999, type="private")
        msg = Message(
            message_id=1,
            date=datetime(2026, 5, 30),
            chat=chat,
            from_user=outsider,
            text=f"/{command}",
        )
        # Tell the Message object what bot it belongs to so PTB doesn't bail.
        msg.set_bot(app.bot)
        update = Update(update_id=1, message=msg)

        matched: list[CommandHandler] = []
        for group in app.handlers.values():
            for h in group:
                if (
                    isinstance(h, CommandHandler)
                    and command in h.commands
                    and h.check_update(update)
                ):
                    matched.append(h)
        assert matched == [], f"non-whitelisted user matched /{command} handlers: {matched}"


class TestRegisterHandlersAttachesErrorHandler:
    def test_global_error_handler_is_registered(self, settings: Settings) -> None:
        # MAJOR-2 pin: register_handlers wires the global error handler.
        app = _build_app(settings)
        assert handlers.global_error_handler in app.error_handlers
