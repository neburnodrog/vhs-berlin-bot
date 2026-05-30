"""End-to-end integration test for the daily-scan pipeline.

Wires every Phase 1-5 component together against:

- A fresh in-memory SQLite ``db.connect(":memory:")``.
- A real ``httpx.AsyncClient`` driven by the ``_FixtureTransport`` from
  ``tests/test_scraper.py`` (replays the captured ``form-initial.html``
  + ``search-district-31-page-{1,2}.html`` fixtures).
- A real ``_UserSubs`` row containing a keyword (``"Spanisch"``) that
  is verified to match exactly two bookable courses in the page-1
  fixture.

The notification fan-out uses an ``AsyncMock`` for ``context.bot.send_message``
so we never make a real Telegram round trip. Everything else is real code.

The test runs ``daily_scan`` three times to pin three integration
contracts in one shot:

1. First scan: matching courses produce ``"new"`` notifications and
   every crawled course lands in ``seen_courses``.
2. Second scan: every course is already in seen_courses with the same
   availability -> ``classify`` returns ``"unchanged"`` -> zero new
   sends, but ``last_seen_at`` MUST be refreshed.
3. Bonus: one course is hand-flipped to ``"belegt"`` in seen_courses
   between scans 2 and 3, so the third scan classifies it as
   ``"back_in_stock"`` and dispatches exactly one extra message.

If the chosen keyword stops matching the fixture (e.g. a fixture refresh
swaps the page-1 results), the assertion at the top of the test will fail
loudly with the count instead of leaking a silently-empty notification
list further down.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx

from vhsbot import db, jobs
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.parser import parse_results_page

FIXTURES = Path(__file__).parent / "fixtures"

_NEXT_BTN_INPUT_RE = re.compile(
    rb'<input[^>]*name="ctl00\$Content\$ILDataGrid1\$ctl01\$ctl04"[^>]*>',
    re.IGNORECASE,
)


def _html_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "text/html; charset=iso-8859-15"},
    )


class _FixtureTransport(httpx.AsyncBaseTransport):
    """Replays the captured 4-stage flow plus a synthetic "no more pages" terminator.

    Inlined rather than imported from ``tests/test_scraper.py`` because the
    ``tests/`` directory has no ``__init__.py`` (and pytest's rootdir-based
    discovery doesn't expose cross-module imports between sibling test files).
    Behaviour is identical: GET form -> POST Erweitert -> POST search ->
    POST page-2 -> POST page-3-with-no-next-arrow.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._next_page_count = 0
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        self._page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        self._page_2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
        self._page_2_stripped = _NEXT_BTN_INPUT_RE.sub(b"", self._page_2)
        assert self._page_2_stripped != self._page_2, "regex must match the next-arrow input"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"btnSearch=Suchen" in body:
            return _html_response(self._page_1)

        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            self._next_page_count += 1
            if self._next_page_count == 1:
                return _html_response(self._page_2)
            return _html_response(self._page_2_stripped)

        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


class _AsyncContextLock:
    """Minimal async-lock used by every other test module here."""

    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _AsyncContextLock:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exit_count += 1


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="testtoken",
        allowed_user_ids=frozenset({42}),
        scan_time=time(hour=8),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=tmp_path / "vhsbot-e2e.db",
        snapshot_dir=tmp_path / "snapshots",
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


def _make_context(
    *, settings: Settings, conn: sqlite3.Connection, client: httpx.AsyncClient
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot_data = {
        BD_SETTINGS: settings,
        BD_DB: conn,
        BD_DB_LOCK: _AsyncContextLock(),
        BD_CLIENT: client,
    }
    return ctx


def _expected_spanisch_match_count() -> int:
    """Count all ``Spanisch`` matches across the page-1+page-2 fixtures.

    Used as the authoritative oracle so the test fails fast if the fixtures
    drift away from carrying any matches at all. Currently 3 (Spanisch C1.1
    online - ">2", Spanisch B1.5 - ">2", Spanisch A1.2 online - "belegt").

    PHASE 6 FINDING: ``jobs.daily_scan`` classifies *every* previously-unseen
    course as ``"new"`` and notifies on it, regardless of current availability.
    The locked design table in ``tasks/todo .md`` says "Strict + back-in-stock:
    **new+bookable** OR full->bookable" — the daily-scan path does NOT enforce
    the "bookable" half for the ``"new"`` branch, whereas ``_run_backfill``
    explicitly skips ``snap.availability not in BOOKABLE_AVAILABILITY``. This
    asymmetry between the two notification paths is surfaced here; the test
    pins current behaviour rather than the spec because Phase 6 is a coverage
    round, not a fix round.
    """
    from vhsbot.matching import matches

    b1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
    b2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
    courses = list(parse_results_page(b1)) + list(parse_results_page(b2))
    hits = [c for c in courses if matches(c, ["Spanisch"])]
    return len(hits)


async def test_end_to_end_daily_scan_three_passes(tmp_path: Path) -> None:
    """Full pipeline: fixtures -> scraper -> classify -> fan-out -> storage.

    This single test wires real components together and asserts:

    - Scan 1: at least one ``"new"`` notification dispatched for a
      keyword we've verified matches the captured fixture; every
      crawled course is recorded in ``seen_courses``.
    - Scan 2 (no change): zero new sends; ``last_seen_at`` refreshed
      on every row.
    - Scan 3 (one course manually marked ``"belegt"`` in storage so
      the fixture's ``">2"`` becomes a transition): exactly one
      ``"back_in_stock"`` notification fired.
    """
    settings = _make_settings(tmp_path)

    # 1. Empty in-memory DB + onboarded user with the keyword we've verified
    #    actually matches the captured page-1 fixture.
    conn = db.connect(":memory:")
    db.init_schema(conn)
    db.upsert_user_settings(conn, user_id=42, districts={31})
    db.add_subscription(conn, user_id=42, keyword="Spanisch")

    # 2. Real httpx client routed through the fixture transport. The
    #    ``follow_redirects=True`` matches production wiring.
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        ctx = _make_context(settings=settings, conn=conn, client=client)

        # --- Scan #1 -------------------------------------------------
        await jobs.daily_scan(ctx)

        # Pin against the fixture's actual content. If this fails,
        # the fixture changed and the rest of the test is meaningless.
        expected_new_matches = _expected_spanisch_match_count()
        assert expected_new_matches >= 1, (
            "fixture must contain at least one Spanisch match; the e2e test relies on this"
        )

        # Every "new" + matching course must have dispatched. The 15/day cap
        # is well above the fixture's count. See the helper's docstring for
        # the Phase 6 finding about "new" not filtering by bookable.
        assert ctx.bot.send_message.await_count == expected_new_matches, (
            f"scan 1 should send {expected_new_matches} notifications "
            f"(Spanisch matches in the fixture), got "
            f"{ctx.bot.send_message.await_count}"
        )
        # Every dispatched message must go to user 42 with MD-V2 parse mode.
        for call in ctx.bot.send_message.await_args_list:
            assert call.kwargs["chat_id"] == 42
            assert call.kwargs.get("parse_mode") == "MarkdownV2"

        # notification_log: every send is recorded with reason="new".
        log_rows = conn.execute("SELECT user_id, kurs_id, reason FROM notification_log").fetchall()
        assert len(log_rows) == expected_new_matches
        for row in log_rows:
            assert row["user_id"] == 42
            assert row["reason"] == "new"

        # seen_courses: every crawled course (20 across both fixture pages)
        # is recorded, NOT only the matching ones.
        seen_count = conn.execute("SELECT COUNT(*) FROM seen_courses").fetchone()[0]
        assert seen_count == 20, (
            f"all 20 fixture courses must land in seen_courses; got {seen_count}"
        )

        # --- Scan #2 (no fixture change -> no new sends) -------------
        ctx.bot.send_message.reset_mock()

        # SQLite's datetime('now') is second-precision; we need a measurable gap
        # before scan 2 to be able to prove last_seen_at actually advanced.
        # Back-date every row by 1 minute and snapshot the values so we can
        # prove scan 2 touched them.
        conn.execute("UPDATE seen_courses SET last_seen_at = datetime('now', '-1 minute')")
        conn.commit()
        prev_last_seen = {
            row["kurs_id"]: row["last_seen_at"]
            for row in conn.execute("SELECT kurs_id, last_seen_at FROM seen_courses").fetchall()
        }

        # Need a fresh transport instance: the previous one's
        # ``_next_page_count`` and ``calls`` carry state from scan 1.
        transport_2 = _FixtureTransport()

    async with httpx.AsyncClient(transport=transport_2, follow_redirects=True) as client:
        ctx = _make_context(settings=settings, conn=conn, client=client)
        await jobs.daily_scan(ctx)

        # No new sends — every course is now ``"unchanged"``.
        assert ctx.bot.send_message.await_count == 0, (
            "scan 2 must not re-send any notifications when nothing changed"
        )
        # notification_log is unchanged.
        log_rows_after_2 = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        assert log_rows_after_2 == expected_new_matches

        # And last_seen_at was bumped on every row.
        new_last_seen = {
            row["kurs_id"]: row["last_seen_at"]
            for row in conn.execute("SELECT kurs_id, last_seen_at FROM seen_courses").fetchall()
        }
        assert set(new_last_seen) == set(prev_last_seen)
        for kurs_id, ts in new_last_seen.items():
            assert ts > prev_last_seen[kurs_id], (
                f"scan 2 must refresh last_seen_at for kurs_id {kurs_id} "
                f"(was {prev_last_seen[kurs_id]!r}, now {ts!r})"
            )

        # --- Scan #3 (force a back-in-stock transition) --------------
        # Pick a course we know is bookable in the fixture AND matched our
        # keyword, then flip its stored availability to "belegt". The next
        # scan will see fixture availability ">2" (or "2"/"1"), so
        # classify -> back_in_stock. We MUST pick a bookable course here —
        # picking one whose fixture availability is "belegt" would result
        # in classify -> "still_full" -> zero new dispatches.
        target_kurs_id = conn.execute(
            "SELECT kurs_id FROM seen_courses "
            "WHERE last_availability != 'belegt' "
            "AND kurs_id IN (SELECT kurs_id FROM notification_log WHERE user_id = 42) "
            "LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE seen_courses SET last_availability = 'belegt' WHERE kurs_id = ?",
            (target_kurs_id,),
        )
        conn.commit()

        ctx.bot.send_message.reset_mock()
        transport_3 = _FixtureTransport()

    async with httpx.AsyncClient(transport=transport_3, follow_redirects=True) as client:
        ctx = _make_context(settings=settings, conn=conn, client=client)
        await jobs.daily_scan(ctx)

        # Exactly one back-in-stock dispatch.
        assert ctx.bot.send_message.await_count == 1, (
            f"scan 3 must dispatch exactly one back-in-stock notification; "
            f"got {ctx.bot.send_message.await_count}"
        )
        # The single dispatched message's text contains the "Back in stock" prefix.
        sent_text = ctx.bot.send_message.await_args.kwargs["text"]
        assert "Back in stock" in sent_text
        # And the notification_log gained one row with reason="back_in_stock".
        new_log_rows = conn.execute(
            "SELECT kurs_id, reason FROM notification_log WHERE reason = 'back_in_stock'"
        ).fetchall()
        assert len(new_log_rows) == 1
        assert new_log_rows[0]["kurs_id"] == target_kurs_id

    conn.close()
