"""End-to-end integration tests for the daily-scan pipeline.

Wires every Phase 1-5 component together against:

- A fresh in-memory SQLite ``db.connect(":memory:")``.
- A real ``httpx.AsyncClient`` driven by the shared ``_FixtureTransport``
  helper from :mod:`tests.conftest` (replays the captured
  ``form-initial.html`` + ``search-district-31-page-{1,2}.html`` fixtures).
- A real ``_UserSubs`` row containing a keyword (``"Spanisch"``) that
  is verified to match two bookable courses in the page-1 fixture.

The notification fan-out uses an ``AsyncMock`` for
``context.bot.send_message`` so we never make a real Telegram round trip.
Everything else is real code.

Three scan passes are exercised, one per test, chained through a small
shared fixture that hands the in-memory connection from the previous
scan into the next:

1. :func:`test_e2e_scan_1_first_seen_dispatches_new_notifications` —
   bookable matching courses produce ``"new"`` notifications, every
   crawled course lands in ``seen_courses``.
2. :func:`test_e2e_scan_2_unchanged_dispatches_nothing` — every course
   is already in ``seen_courses`` with the same availability so
   ``classify`` returns ``"unchanged"``; zero new sends but
   ``last_seen_at`` MUST be refreshed.
3. :func:`test_e2e_scan_3_back_in_stock_dispatches_one_notification`
   — one course is hand-flipped to ``"belegt"`` in ``seen_courses``
   between scans 2 and 3, so the third scan classifies it as
   ``"back_in_stock"`` and dispatches exactly one extra message.

If the chosen keyword stops matching the fixture (e.g. a fixture refresh
swaps the page-1 results), the assertion at the top of the scan-1 test
will fail loudly with the count instead of leaking a silently-empty
notification list further down.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import FIXTURES, _FixtureTransport, _make_context, set_last_availability

from vhsbot import db, jobs
from vhsbot.config import Settings
from vhsbot.parser import parse_results_page


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


def _expected_match_count_in_fixture(keyword: str) -> int:
    """Count ``keyword`` matches that are also BOOKABLE across the two pages.

    Used as the authoritative oracle so the test fails fast if the
    fixtures drift away from carrying any matches. Mirrors the locked
    design's "new+bookable" notification policy: after the Phase 6
    review-pass fix, ``daily_scan`` filters new sightings by
    ``BOOKABLE_AVAILABILITY`` symmetrically with ``_run_backfill``, so the
    e2e oracle does the same.

    Currently expects 2 matches for ``"Spanisch"`` (Spanisch C1.1 online
    - ``">2"``, Spanisch B1.5 - ``">2"``). Spanisch A1.2 online is in
    the fixture as ``"belegt"`` and so is intentionally NOT counted.
    """
    from vhsbot.db import BOOKABLE_AVAILABILITY
    from vhsbot.matching import matches

    b1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
    b2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
    courses = list(parse_results_page(b1)) + list(parse_results_page(b2))
    hits = [c for c in courses if matches(c, [keyword]) and c.availability in BOOKABLE_AVAILABILITY]
    return len(hits)


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    return _make_settings(tmp_path)


@pytest.fixture
def e2e_conn() -> Iterator[sqlite3.Connection]:
    """A fresh in-memory connection per test; stage fixtures replay prior scans on top of it.

    Seeded with user 42 watching ``"Spanisch"``. Function-scoped: each
    test starts from a clean database. Tests that need the post-scan-1
    state pull the ``after_scan_1`` stage fixture below.
    """
    conn = db.connect(":memory:")
    db.init_schema(conn)
    db.upsert_user_settings(conn, user_id=42, districts={31})
    db.add_subscription(conn, user_id=42, keyword="Spanisch")
    yield conn
    conn.close()


@pytest.fixture
async def after_scan_1(e2e_settings: Settings, e2e_conn: sqlite3.Connection) -> sqlite3.Connection:
    """Run scan 1 and return the seeded connection.

    Downstream tests consume this fixture so scans 2 / 3 build on scan
    1's persisted state without each test re-running the first scan
    inline. Scans 2 and 3 themselves are run inline by their tests —
    making each test's setup transparent at the call site.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        ctx = _make_context(settings=e2e_settings, conn=e2e_conn, client=client)
        await jobs.daily_scan(ctx)
    return e2e_conn


# ---------------------------------------------------------------------------
# Scan 1: first sightings -> "new" + bookable -> dispatches
# ---------------------------------------------------------------------------


async def test_e2e_scan_1_first_seen_dispatches_new_notifications(
    e2e_settings: Settings, e2e_conn: sqlite3.Connection
) -> None:
    """First scan: bookable matching courses dispatch as ``"new"``; every
    crawled course is recorded in ``seen_courses`` regardless of match."""
    expected_matches = _expected_match_count_in_fixture("Spanisch")
    assert expected_matches >= 1, (
        "fixture must contain at least one bookable Spanisch match; the e2e tests rely on this"
    )

    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        ctx = _make_context(settings=e2e_settings, conn=e2e_conn, client=client)
        await jobs.daily_scan(ctx)

        # Every "new" + matching + bookable course must have dispatched. The
        # 15/day cap is well above the fixture's count.
        assert ctx.bot.send_message.await_count == expected_matches, (
            f"scan 1 should send {expected_matches} notifications "
            f"(bookable Spanisch matches in the fixture), got "
            f"{ctx.bot.send_message.await_count}"
        )
        # Every dispatched message must go to user 42 with MD-V2 parse mode.
        for call in ctx.bot.send_message.await_args_list:
            assert call.kwargs["chat_id"] == 42
            assert call.kwargs.get("parse_mode") == "MarkdownV2"

    # notification_log: every send is recorded with reason="new".
    log_rows = e2e_conn.execute("SELECT user_id, kurs_id, reason FROM notification_log").fetchall()
    assert len(log_rows) == expected_matches
    for row in log_rows:
        assert row["user_id"] == 42
        assert row["reason"] == "new"

    # seen_courses: every crawled course (20 across both fixture pages)
    # is recorded, NOT only the matching ones — the new+belegt branch
    # also upserts (so the next scan can detect back_in_stock).
    seen_count = e2e_conn.execute("SELECT COUNT(*) FROM seen_courses").fetchone()[0]
    assert seen_count == 20, f"all 20 fixture courses must land in seen_courses; got {seen_count}"


# ---------------------------------------------------------------------------
# Scan 2: no fixture change -> "unchanged" -> nothing dispatched
# ---------------------------------------------------------------------------


async def test_e2e_scan_2_unchanged_dispatches_nothing(
    e2e_settings: Settings, after_scan_1: sqlite3.Connection
) -> None:
    """Second scan over the same fixture: every course classified ``"unchanged"``.

    Zero sends but ``last_seen_at`` MUST be refreshed on every row.
    """
    expected_matches = _expected_match_count_in_fixture("Spanisch")

    # Backdate every row by 1 minute and snapshot the values so we can
    # prove scan 2 touched them. (SQLite's datetime('now') is
    # second-precision; we need a measurable gap.)
    after_scan_1.execute("UPDATE seen_courses SET last_seen_at = datetime('now', '-1 minute')")
    after_scan_1.commit()
    prev_last_seen = {
        row["kurs_id"]: row["last_seen_at"]
        for row in after_scan_1.execute("SELECT kurs_id, last_seen_at FROM seen_courses").fetchall()
    }

    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        ctx = _make_context(settings=e2e_settings, conn=after_scan_1, client=client)
        await jobs.daily_scan(ctx)

        # No new sends — every course is now "unchanged".
        assert ctx.bot.send_message.await_count == 0, (
            "scan 2 must not re-send any notifications when nothing changed"
        )

    # notification_log unchanged: only scan-1's rows persist.
    log_rows_after_2 = after_scan_1.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
    assert log_rows_after_2 == expected_matches

    # last_seen_at was bumped on every row.
    new_last_seen = {
        row["kurs_id"]: row["last_seen_at"]
        for row in after_scan_1.execute("SELECT kurs_id, last_seen_at FROM seen_courses").fetchall()
    }
    assert set(new_last_seen) == set(prev_last_seen)
    for kurs_id, ts in new_last_seen.items():
        assert ts > prev_last_seen[kurs_id], (
            f"scan 2 must refresh last_seen_at for kurs_id {kurs_id} "
            f"(was {prev_last_seen[kurs_id]!r}, now {ts!r})"
        )


# ---------------------------------------------------------------------------
# Scan 3: hand-flip seen_courses to "belegt" -> back_in_stock dispatches
# ---------------------------------------------------------------------------


async def test_e2e_scan_3_back_in_stock_dispatches_one_notification(
    e2e_settings: Settings, after_scan_1: sqlite3.Connection
) -> None:
    """Force a back-in-stock transition on a previously-notified course.

    Consumes the ``after_scan_1`` stage fixture (post-scan-1 state) and
    runs scan 2 + scan 3 inline so the full chain is transparent at the
    call site. Scan 2 leaves availability unchanged; between scans 2 and
    3 we hand-flip one course's stored availability to ``"belegt"`` so
    scan 3 classifies it as ``back_in_stock``.

    Pick a course we know is bookable in the fixture AND matched our
    keyword, then flip its stored availability to ``"belegt"``. The next
    scan will see fixture availability ``">2"``/``"2"``/``"1"``, so
    ``classify`` -> ``back_in_stock``. We MUST pick a bookable course
    here — picking one whose fixture availability is ``"belegt"`` would
    classify -> ``"still_full"`` -> zero new dispatches.
    """
    # Scan 2 inline: same fixture, nothing changes -> zero new dispatches.
    after_scan_1.execute("UPDATE seen_courses SET last_seen_at = datetime('now', '-1 minute')")
    after_scan_1.commit()
    transport_2 = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport_2, follow_redirects=True) as client:
        ctx_2 = _make_context(settings=e2e_settings, conn=after_scan_1, client=client)
        await jobs.daily_scan(ctx_2)

    # Between scans 2 and 3: hand-flip a bookable-in-fixture course to belegt.
    target_kurs_id = after_scan_1.execute(
        "SELECT kurs_id FROM seen_courses "
        "WHERE last_availability != 'belegt' "
        "AND kurs_id IN (SELECT kurs_id FROM notification_log WHERE user_id = 42) "
        "LIMIT 1"
    ).fetchone()[0]
    set_last_availability(after_scan_1, kurs_id=target_kurs_id, availability="belegt")

    transport_3 = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport_3, follow_redirects=True) as client:
        ctx_3 = _make_context(settings=e2e_settings, conn=after_scan_1, client=client)
        await jobs.daily_scan(ctx_3)

        # Exactly one back-in-stock dispatch from scan 3.
        assert ctx_3.bot.send_message.await_count == 1, (
            f"scan 3 must dispatch exactly one back-in-stock notification; "
            f"got {ctx_3.bot.send_message.await_count}"
        )
        # The single dispatched message's text contains the "Back in stock" prefix.
        sent_text = ctx_3.bot.send_message.await_args.kwargs["text"]
        assert "Back in stock" in sent_text

    # notification_log gained one row with reason="back_in_stock".
    new_log_rows = after_scan_1.execute(
        "SELECT kurs_id, reason FROM notification_log WHERE reason = 'back_in_stock'"
    ).fetchall()
    assert len(new_log_rows) == 1
    assert new_log_rows[0]["kurs_id"] == target_kurs_id
