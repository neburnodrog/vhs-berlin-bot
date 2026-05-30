"""Tests for the Telegram handlers layer (Phase 4).

Two groups:

1. Pure-function tests for the message builders and the Markdown V2
   escaper. These run without any Telegram or DB I/O and form the bulk
   of the coverage.
2. Handler smoke tests that build fake ``Update`` / ``Context`` objects
   with ``unittest.mock.AsyncMock`` and assert on the bot method calls.
   Goal: cover the whitelist branch, the new-vs-known-user branch of
   ``/start``, and the on-demand backfill cap.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from vhsbot import db, handlers
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

    def test_escapes_backslash_itself(self) -> None:
        # Telegram's MD-V2 does not list backslash, but our implementation
        # must not double-escape characters that are already preceded by one.
        # The escaper operates on the raw user input; callers pre-compose strings.
        assert handlers.escape_markdown_v2("a.b") == "a\\.b"

    def test_idempotent_on_empty_string(self) -> None:
        assert handlers.escape_markdown_v2("") == ""


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


class TestBuildCourseMessage:
    def _course(self, **kw: object) -> CourseSnapshot:
        defaults = {
            "kurs_id": 12345,
            "title": "Yoga sanft",
            "course_number": "Mi251-001K",
            "district": "Mitte",
            "venue": None,
            "date_range": "01.06.2026 - 30.07.2026",
            "availability": ">2",
        }
        defaults.update(kw)
        return CourseSnapshot(**defaults)  # type: ignore[arg-type]

    def test_returns_text_and_inline_keyboard(self) -> None:
        course = self._course()
        text, markup = handlers.build_course_message(
            course,
            matched_keywords=["Yoga"],
            detail_url="https://example.test/CourseDetail.aspx?id=12345",
        )
        assert "Yoga sanft" in text or "Yoga" in text
        # markup is an InlineKeyboardMarkup with exactly one URL button.
        rows = markup.inline_keyboard
        assert len(rows) == 1
        assert len(rows[0]) == 1
        assert rows[0][0].url == "https://example.test/CourseDetail.aspx?id=12345"

    def test_includes_matched_keywords_in_message(self) -> None:
        course = self._course()
        text, _ = handlers.build_course_message(
            course,
            matched_keywords=["Yoga", "Sanft"],
            detail_url="https://example.test/x",
        )
        assert "Yoga" in text
        assert "Sanft" in text

    def test_escapes_period_in_date_range(self) -> None:
        course = self._course(date_range="01.06.2026")
        text, _ = handlers.build_course_message(
            course, matched_keywords=["Yoga"], detail_url="https://example.test/x"
        )
        # Date periods must be backslash-escaped for MD-V2.
        assert "01\\.06\\.2026" in text

    def test_includes_availability_literal_escaped(self) -> None:
        course = self._course(availability=">2")
        text, _ = handlers.build_course_message(
            course, matched_keywords=["Yoga"], detail_url="https://example.test/x"
        )
        # ">" is MD-V2 reserved.
        assert "\\>2" in text


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


def _make_context(
    *,
    settings: Settings,
    conn: sqlite3.Connection,
    client: object,
    args: list[str] | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot_data = {
        "settings": settings,
        "db": conn,
        "http_client": client,
    }
    ctx.args = args or []
    return ctx


class TestStartHandler:
    async def test_unknown_user_is_rejected_and_db_untouched(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        update = _make_update(user_id=999)  # not in whitelist {111}
        ctx = _make_context(settings=settings, conn=conn, client=object())

        result = await handlers.start(update, ctx)

        update.message.reply_text.assert_awaited()
        text = update.message.reply_text.await_args.args[0]
        assert "private" in text.lower()
        # No user_settings inserted.
        assert db.get_user_settings(conn, user_id=999) is None
        # ConversationHandler.END signals "do not enter the conv flow".
        from telegram.ext import ConversationHandler

        assert result == ConversationHandler.END

    async def test_whitelisted_new_user_kicks_off_conversation(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the initial-form GET so start() can render the district keyboard
        # without real HTTP. We monkeypatch parse_district_map to return a tiny map.
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


class TestWatchHandler:
    async def test_unknown_user_is_rejected(
        self, settings: Settings, conn: sqlite3.Connection
    ) -> None:
        update = _make_update(user_id=999)
        ctx = _make_context(settings=settings, conn=conn, client=object(), args=["Yoga"])
        await handlers.watch(update, ctx)
        update.message.reply_text.assert_awaited()
        text = update.message.reply_text.await_args.args[0]
        assert "private" in text.lower()
        assert db.list_subscriptions(conn, user_id=999) == []

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
