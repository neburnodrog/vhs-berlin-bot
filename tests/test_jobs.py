"""Tests for the Phase 5 daily-scan orchestration.

We exercise ``jobs.daily_scan`` against a hand-rolled ``_FakeContext``
that mimics enough of PTB's ``ContextTypes.DEFAULT_TYPE`` for the
orchestrator to run: a bot with an ``AsyncMock`` ``send_message``, a
shared ``bot_data`` carrying settings, an in-memory sqlite connection
seeded per test, and the ``BD_DB_LOCK`` async-context lock from the
Phase 4 handler tests.

``scraper.crawl`` is monkeypatched per test to return a curated list of
``CourseSnapshot`` objects. We deliberately don't go through the real
HTTP transport here — the scraper itself is tested in
``tests/test_scraper.py``; the daily-scan tests pin behaviour above
that boundary.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import _make_context

from vhsbot import db, jobs
from vhsbot.config import Settings
from vhsbot.db import CourseSnapshot
from vhsbot.scraper import CrawlResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="testtoken",
        allowed_user_ids=frozenset({111, 222}),
        scan_time=time(hour=8),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=tmp_path / "vhsbot-test.db",
        snapshot_dir=tmp_path / "snapshots",
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


@pytest.fixture
def frozen_24h_cutoff(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``_since_24h_iso`` to a value computed from SQLite ``datetime('now')``.

    The cap-boundary tests insert a row with SQLite ``datetime('now')``
    then backdate it; ``daily_scan`` then computes the trailing-24h
    cutoff in Python via ``datetime.now(UTC)``. Without freezing, the
    second call advances by some number of milliseconds — usually
    harmless, but on a slow CI runner the boundary row at exactly
    23h59m59s ago can land on either side of the cutoff and flake.

    We snapshot a single SQLite ``datetime('now', '-24 hours')`` at
    test-setup time and force ``jobs._since_24h_iso`` to return that
    fixed string for the entire test, so both sides of the comparison
    use the same reference instant.
    """
    cutoff = conn.execute("SELECT datetime('now', '-24 hours')").fetchone()[0]
    monkeypatch.setattr(jobs, "_since_24h_iso", lambda _now: cutoff)
    return cutoff


def _snap(**kw: Any) -> CourseSnapshot:
    base: dict[str, Any] = {
        "kurs_id": 1000,
        "title": "Yoga sanft",
        "course_number": "Mi251-001K",
        "district": "Mitte",
        "venue": None,
        "date_range": "01.06.2026",
        "availability": ">2",
    }
    base.update(kw)
    return CourseSnapshot(**base)


def _patch_crawl(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[CourseSnapshot],
    *,
    capture_callback: list[tuple[int, int, bytes]] | None = None,
) -> None:
    """Replace ``scraper.crawl_district`` so daily_scan returns ``snapshots``.

    Phase 5-review refactor: ``daily_scan`` now drives ``crawl_district``
    per-district itself (so a single failing district doesn't lose every
    other district's seen_courses upserts). The patch returns the same
    ``snapshots`` list for the FIRST district visited and ``[]`` for the
    rest, which preserves the pre-refactor test semantics (snapshots get
    classified + fanned out exactly once).

    If ``capture_callback`` is provided, the substitute also invokes the
    raw-HTML callback once per district with synthetic
    ``(district, page, html)`` triples so the snapshot-writer side of
    ``daily_scan`` can be exercised without going through the real scraper.
    """
    delivered: dict[int, bool] = {}

    async def fake_crawl_district(
        *,
        client: object,
        district_checkbox_index: int,
        sleep_seconds: float,
        district_id: int | None = None,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> CrawlResult:
        assert district_id is not None
        if raw_html_callback is not None:
            html = b"<html>page-0 for district " + str(district_id).encode() + b"</html>"
            raw_html_callback(district_id, 0, html)
            if capture_callback is not None:
                capture_callback.append((district_id, 0, b"<html>"))
        # Hand the snapshots to the FIRST district visited only.
        if not delivered:
            delivered[district_id] = True
            return CrawlResult(snapshots=tuple(snapshots), truncated=False)
        return CrawlResult(snapshots=(), truncated=False)

    monkeypatch.setattr(jobs.scraper, "crawl_district", fake_crawl_district)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        # Cover the typical district set used by the tests; harmless extras.
        return {31: 5, 32: 2, 33: 3, 38: 4, 39: 6}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)


# ---------------------------------------------------------------------------
# 1. Skip when no active users
# ---------------------------------------------------------------------------


async def test_daily_scan_skips_when_no_active_users(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No user_settings rows -> union_active_districts() returns empty.
    crawl_called: list[bool] = []

    async def boom_crawl(**kw: Any) -> CrawlResult:
        crawl_called.append(True)
        return CrawlResult(snapshots=(), truncated=False)

    monkeypatch.setattr(jobs.scraper, "crawl", boom_crawl)
    ctx = _make_context(settings=settings, conn=conn)
    await jobs.daily_scan(ctx)

    assert crawl_called == []  # never called
    ctx.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Classify against seen state
# ---------------------------------------------------------------------------


async def test_daily_scan_classifies_each_course_against_seen_state(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed: user 111 watches "Yoga" in district 31; one course previously
    # seen as "belegt". Crawl returns that course now bookable -> notify.
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.mark_seen(conn, _snap(kurs_id=2000, title="Yoga sanft", availability="belegt"))

    snapshots = [_snap(kurs_id=2000, title="Yoga sanft", availability=">2")]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert ctx.bot.send_message.await_count == 1
    text = ctx.bot.send_message.await_args.kwargs["text"]
    assert "Back in stock" in text
    # And notification_log got a "back_in_stock" row.
    rows = conn.execute("SELECT user_id, kurs_id, reason FROM notification_log").fetchall()
    assert [(r["user_id"], r["kurs_id"], r["reason"]) for r in rows] == [
        (111, 2000, "back_in_stock")
    ]


# ---------------------------------------------------------------------------
# 3. Dispatches only to matching users
# ---------------------------------------------------------------------------


async def test_daily_scan_dispatches_to_only_matching_users(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.upsert_user_settings(conn, user_id=222, districts=[31])
    db.add_subscription(conn, user_id=222, keyword="Tango")

    snapshots = [_snap(kurs_id=3000, title="Yoga sanft", availability=">2")]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # Only user 111 (Yoga) should have received a message.
    assert ctx.bot.send_message.await_count == 1
    sent_chat_id = ctx.bot.send_message.await_args.kwargs["chat_id"]
    assert sent_chat_id == 111


# ---------------------------------------------------------------------------
# 4. Paused user gets nothing
# ---------------------------------------------------------------------------


async def test_daily_scan_respects_paused_flag(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.set_paused(conn, user_id=111, paused=True)

    snapshots = [_snap(kurs_id=4000, title="Yoga sanft", availability=">2")]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # Paused user is filtered out of union_active_districts -> no crawl
    # is triggered at all, but even if it were, the paused user must not
    # receive a notification.
    ctx.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Cross-day cap
# ---------------------------------------------------------------------------


async def test_daily_scan_enforces_cross_day_cap(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Seed: 14 notification_log rows in the last 24h for user 111.
    for i in range(14):
        db.record_notification(conn, user_id=111, kurs_id=9000 + i, reason="new")

    # Crawl returns 10 new matches.
    snapshots = [_snap(kurs_id=5000 + i, title=f"Yoga {i}", availability=">2") for i in range(10)]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # The cap is 15: 14 already + 1 new = 15. So exactly 1 message.
    assert ctx.bot.send_message.await_count == 1


# ---------------------------------------------------------------------------
# 6. seen_courses upserted regardless
# ---------------------------------------------------------------------------


async def test_daily_scan_upserts_all_seen_courses_regardless_of_notification(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Seed: course 6000 already seen as ">2" (unchanged classification),
    # course 6001 already seen as ">2" (also unchanged) — neither will trigger.
    db.mark_seen(conn, _snap(kurs_id=6000, title="Yoga 1", availability=">2"))
    db.mark_seen(conn, _snap(kurs_id=6001, title="Yoga 2", availability=">2"))

    snapshots = [
        _snap(kurs_id=6000, title="Yoga 1", availability=">2"),
        _snap(kurs_id=6001, title="Yoga 2", availability=">2"),
    ]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # Both courses still present in seen_courses (and last_seen_at refreshed).
    assert db.get_seen_course(conn, kurs_id=6000) is not None
    assert db.get_seen_course(conn, kurs_id=6001) is not None
    # No notifications fired.
    ctx.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Snapshot HTML persistence
# ---------------------------------------------------------------------------


async def test_daily_scan_persists_raw_html_per_page(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31, 32])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    capture: list[tuple[int, int, bytes]] = []
    _patch_crawl(monkeypatch, [], capture_callback=capture)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # Today's snapshot dir should contain a file per district written by
    # the callback. We don't pin the exact date — derive it from the
    # settings.tz so the test is robust to timezone-borderline runs.
    from datetime import datetime as _dt

    today = _dt.now(settings.tz).date().isoformat()
    day_dir = settings.snapshot_dir / today
    assert day_dir.exists()
    files = sorted(p.name for p in day_dir.iterdir())
    # One file per district (the fake crawl writes (district, page_idx=0))
    assert files == ["31-page-0.html", "32-page-0.html"]
    # And each file actually contains bytes.
    for f in day_dir.iterdir():
        assert f.read_bytes()


# ---------------------------------------------------------------------------
# 8. Snapshot pruning
# ---------------------------------------------------------------------------


async def test_daily_scan_prunes_old_snapshot_directories(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Pre-create three date-named directories:
    #  - 8 days ago: must be deleted
    #  - 6 days ago: must survive
    #  - a non-date directory: must survive
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    today = _dt.now(settings.tz).date()
    eight_days = today - _td(days=8)
    six_days = today - _td(days=6)

    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    (settings.snapshot_dir / eight_days.isoformat()).mkdir()
    (settings.snapshot_dir / eight_days.isoformat() / "31-page-0.html").write_bytes(b"old")
    (settings.snapshot_dir / six_days.isoformat()).mkdir()
    (settings.snapshot_dir / six_days.isoformat() / "31-page-0.html").write_bytes(b"recent")
    (settings.snapshot_dir / "not-a-date").mkdir()
    (settings.snapshot_dir / "not-a-date" / "foo.txt").write_bytes(b"keep me")

    _patch_crawl(monkeypatch, [])
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert not (settings.snapshot_dir / eight_days.isoformat()).exists(), (
        "directory older than 7 days must be pruned"
    )
    assert (settings.snapshot_dir / six_days.isoformat()).exists(), (
        "directory 6 days old must survive"
    )
    assert (settings.snapshot_dir / "not-a-date").exists(), (
        "non-date-named directory must be left alone"
    )


# ---------------------------------------------------------------------------
# Bonus: exceptions propagate for the global error handler
# ---------------------------------------------------------------------------


async def test_daily_scan_reraises_exception_for_global_handler(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    async def boom_crawl(**kw: Any) -> CrawlResult:
        raise RuntimeError("VHS Berlin returned 503")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom_crawl)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError, match="503"):
        await jobs.daily_scan(ctx)


# ---------------------------------------------------------------------------
# BLOCKER-1: cap counter must not double-count
# ---------------------------------------------------------------------------


async def test_cap_counter_does_not_double_count_after_in_scan_send(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13 prior notifications + 5 in-scan matches = exactly 2 sends (cap=15).

    The pre-fix implementation re-queried ``count_notifications_since``
    AFTER each ``record_notification`` insert AND also bumped its in-scan
    counter, double-counting the same send. Effective per-user budget was
    7-8 instead of 15. The fix snapshots the prior count once per user.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    for i in range(13):
        db.record_notification(conn, user_id=111, kurs_id=9000 + i, reason="new")

    # 5 fresh matches; cap = 15; 13 prior leaves room for 2 sends.
    snapshots = [_snap(kurs_id=5000 + i, title=f"Yoga {i}", availability=">2") for i in range(5)]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert ctx.bot.send_message.await_count == 2, (
        "13 prior + cap=15 must allow exactly 2 sends; double-counting yields 1"
    )


# ---------------------------------------------------------------------------
# MAJOR-2: per-user district filtering
# ---------------------------------------------------------------------------


async def test_daily_scan_filters_by_user_districts(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User A subscribed to district 31 only; course from district 32 reaches no one but B."""
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.upsert_user_settings(conn, user_id=222, districts=[32])
    db.add_subscription(conn, user_id=222, keyword="Yoga")

    # Snapshot from district 32 only. User A (district 31) must NOT receive it.
    snap_d32 = _snap(kurs_id=7000, title="Yoga sanft", availability=">2", district="Spandau")
    _patch_crawl_district(monkeypatch, {31: [], 32: [snap_d32]})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # Only user 222 should have received a message — user 111 is filtered out.
    chat_ids = [call.kwargs["chat_id"] for call in ctx.bot.send_message.await_args_list]
    assert 111 not in chat_ids, "user A (district 31 only) must not receive district-32 course"
    assert chat_ids == [222]


# ---------------------------------------------------------------------------
# MAJOR-6: mid-scrape crash data loss
# ---------------------------------------------------------------------------


async def test_daily_scan_partial_district_failure_persists_completed_state(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If district 32 raises mid-scan, district 31's seen_courses rows must still be persisted."""
    db.upsert_user_settings(conn, user_id=111, districts=[31, 32])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    snap_d31 = _snap(kurs_id=8000, title="Yoga sanft", availability=">2", district="Mitte")

    async def per_district(
        *,
        client: object,
        district_checkbox_index: int,
        sleep_seconds: float,
        district_id: int | None = None,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> CrawlResult:
        if district_id == 31:
            return CrawlResult(snapshots=(snap_d31,), truncated=False)
        raise RuntimeError("district 32 exploded")

    # Monkeypatch BOTH crawl_district and the district map indirection used by crawl.
    monkeypatch.setattr(jobs.scraper, "crawl_district", per_district)

    # daily_scan needs to know how to map district_id -> checkbox_index.
    # Stub the district-map fetcher so we don't need a real GET.
    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5, 32: 2}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)

    ctx = _make_context(settings=settings, conn=conn)

    # The scan must re-raise so PTB's error handler sees the failure,
    # AND it must have persisted district 31's snapshot before doing so.
    with pytest.raises(RuntimeError, match="district 32"):
        await jobs.daily_scan(ctx)

    seen = db.get_seen_course(conn, kurs_id=8000)
    assert seen is not None, (
        "district 31's snapshot must be in seen_courses despite district 32's failure"
    )


def _patch_crawl_district(
    monkeypatch: pytest.MonkeyPatch,
    per_district_snapshots: dict[int, list[CourseSnapshot]],
) -> None:
    """Patch scraper.crawl_district + the district-map fetcher.

    ``per_district_snapshots`` maps district_id -> snapshots to return for
    that district. Any district not in the map returns ``[]``.

    Also patches ``jobs._fetch_district_map`` so the scan can map
    district_id -> checkbox_index without a real HTTP GET. The checkbox
    indices are synthetic (district_id // 6) but the test only cares about
    the resulting per-district fan-out.
    """

    async def per_district(
        *,
        client: object,
        district_checkbox_index: int,
        sleep_seconds: float,
        district_id: int | None = None,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> CrawlResult:
        assert district_id is not None
        if raw_html_callback is not None:
            raw_html_callback(district_id, 0, b"<html>fake</html>")
        return CrawlResult(
            snapshots=tuple(per_district_snapshots.get(district_id, [])), truncated=False
        )

    monkeypatch.setattr(jobs.scraper, "crawl_district", per_district)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        # Synthetic mapping: every requested district gets some non-zero index.
        return {d: d % 16 for d in per_district_snapshots} or {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)


# ---------------------------------------------------------------------------
# Snapshot writer error handling
# ---------------------------------------------------------------------------


async def test_snapshot_writer_logs_and_continues_on_oserror(
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``path.write_bytes`` raising OSError must NOT abort the scan."""
    import logging as _logging

    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Force write_bytes to raise.
    original_write = Path.write_bytes

    def boom_write(self: Path, data: bytes) -> int:
        if self.suffix == ".html":
            raise OSError("disk full")
        return original_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", boom_write)

    _patch_crawl_district(monkeypatch, {31: [_snap(kurs_id=9100, availability=">2")]})
    ctx = _make_context(settings=settings, conn=conn)

    with caplog.at_level(_logging.WARNING):
        # Must NOT raise — snapshot writes are debug-only.
        await jobs.daily_scan(ctx)

    assert any("snapshot write failed" in rec.message for rec in caplog.records)


async def test_prune_continues_on_permission_error(
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``shutil.rmtree`` raising OSError must log + continue, not crash the scan."""
    import logging as _logging

    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    from datetime import datetime as _dt
    from datetime import timedelta as _td

    today = _dt.now(settings.tz).date()
    eight_days = today - _td(days=8)
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    (settings.snapshot_dir / eight_days.isoformat()).mkdir()

    import shutil as _shutil

    original = _shutil.rmtree

    def boom_rmtree(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(_shutil, "rmtree", boom_rmtree)
    _patch_crawl_district(monkeypatch, {31: []})
    ctx = _make_context(settings=settings, conn=conn)

    with caplog.at_level(_logging.WARNING):
        # Must NOT raise.
        await jobs.daily_scan(ctx)

    assert any("snapshot prune failed" in rec.message for rec in caplog.records)
    # Restore so other tests aren't affected (monkeypatch undoes this anyway).
    monkeypatch.setattr(_shutil, "rmtree", original)


async def test_prune_deletes_directory_exactly_7_days_old(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a dir exactly ``today - 7d`` old must be pruned (matches `weekly`)."""
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    from datetime import datetime as _dt
    from datetime import timedelta as _td

    today = _dt.now(settings.tz).date()
    seven_days = today - _td(days=7)

    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    (settings.snapshot_dir / seven_days.isoformat()).mkdir()
    (settings.snapshot_dir / seven_days.isoformat() / "31-page-0.html").write_bytes(b"old")

    _patch_crawl_district(monkeypatch, {31: []})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert not (settings.snapshot_dir / seven_days.isoformat()).exists(), (
        "directory exactly 7 days old must be pruned (weekly cleanup semantics)"
    )


# ---------------------------------------------------------------------------
# Phase 6 additions: cap-window boundary + multi-user + still_full + dir create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_from_cutoff", "expected_sends", "rationale"),
    [
        pytest.param(
            "+1 second",
            0,
            "boundary row 1s ABOVE the cutoff (=inside window) must count toward the 15-msg cap",
            id="inside_window_blocks_send",
        ),
        pytest.param(
            "-1 second",
            1,
            "boundary row 1s BELOW the cutoff (=outside window) must NOT count; cap allows 1 send",
            id="outside_window_allows_send",
        ),
    ],
)
async def test_cap_window_boundary(
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    frozen_24h_cutoff: str,
    offset_from_cutoff: str,
    expected_sends: int,
    rationale: str,
) -> None:
    """Pair-test: a row JUST inside the trailing-24h window counts; just outside doesn't.

    Both cases seed 14 fresh priors + 1 boundary row positioned relative
    to the frozen cutoff string. The cap is 15: with the boundary row
    inside, the cap is hit and a fresh match must NOT dispatch; with the
    boundary row outside, the cap counter sees 14 and the fresh match
    DOES dispatch.

    Crucially, both sides of the ``>=`` comparison derive from the same
    instant: the ``frozen_24h_cutoff`` fixture pins ``jobs._since_24h_iso``
    to a SQLite-snapshotted reference, AND the boundary row's ``sent_at``
    is computed as ``datetime(cutoff, offset)`` — NOT against
    ``datetime('now')`` — so wall-clock drift between fixture setup and
    the test body cannot flip the boundary across the cutoff. Eliminates
    the sub-second flake window the previous "now"-based test had.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    for i in range(14):
        db.record_notification(conn, user_id=111, kurs_id=9000 + i, reason="new")
    db.record_notification(conn, user_id=111, kurs_id=9100, reason="new")
    # Position the boundary row relative to the frozen cutoff, NOT to
    # 'now', so both sides of the comparison share one reference instant.
    conn.execute(
        "UPDATE notification_log SET sent_at = datetime(?, ?) WHERE kurs_id = 9100",
        (frozen_24h_cutoff, offset_from_cutoff),
    )
    conn.commit()

    snapshots = [_snap(kurs_id=5000, title="Yoga sanft", availability=">2")]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert ctx.bot.send_message.await_count == expected_sends, rationale


async def test_cap_exactly_at_15_priors_blocks_all_new_matches(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 15 priors already in-window, every fresh match is blocked."""
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    for i in range(15):
        db.record_notification(conn, user_id=111, kurs_id=9000 + i, reason="new")

    snapshots = [_snap(kurs_id=5000 + i, title=f"Yoga {i}", availability=">2") for i in range(5)]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert ctx.bot.send_message.await_count == 0, "15 priors == cap; no fresh matches must dispatch"


async def test_two_users_match_same_course_each_logged_with_own_cap(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two users with overlapping keywords get independent log rows + caps.

    One course matches both; each user receives exactly one message and
    each user's notification_log row is recorded independently.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.upsert_user_settings(conn, user_id=222, districts=[31])
    db.add_subscription(conn, user_id=222, keyword="Yoga")

    snapshots = [_snap(kurs_id=6000, title="Yoga sanft", availability=">2")]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    chat_ids = sorted(call.kwargs["chat_id"] for call in ctx.bot.send_message.await_args_list)
    assert chat_ids == [111, 222]

    # Each user has their own notification_log row.
    rows = conn.execute(
        "SELECT user_id FROM notification_log WHERE kurs_id = 6000 AND reason = 'new' "
        "ORDER BY user_id"
    ).fetchall()
    assert [r["user_id"] for r in rows] == [111, 222]


async def test_user_a_at_cap_user_b_unaffected(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User A is at cap; user B has 0 priors. Same course matches both.

    User A receives nothing; user B receives exactly one message. Pins
    that the cap is applied PER-USER, never globally.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    db.upsert_user_settings(conn, user_id=222, districts=[31])
    db.add_subscription(conn, user_id=222, keyword="Yoga")

    # User A: 15 priors -> at cap.
    for i in range(15):
        db.record_notification(conn, user_id=111, kurs_id=9000 + i, reason="new")
    # User B: 0 priors.

    snapshots = [_snap(kurs_id=6500, title="Yoga sanft", availability=">2")]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    chat_ids = [call.kwargs["chat_id"] for call in ctx.bot.send_message.await_args_list]
    assert chat_ids == [222], (
        f"user A at cap must receive 0; user B must receive 1; got chat_ids={chat_ids}"
    )


async def test_active_user_with_no_keywords_receives_nothing(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An onboarded user with no /watch subscriptions gets no notifications.

    The user has ``user_settings`` (so they're "active" / non-paused) but
    zero rows in ``subscriptions``. The fan-out must skip them entirely
    even when courses are streaming through the scan.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    # NO add_subscription call.

    snapshots = [
        _snap(kurs_id=7000 + i, title=f"Some course {i}", availability=">2") for i in range(3)
    ]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    ctx.bot.send_message.assert_not_called()


async def test_snapshot_dir_created_when_missing(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If ``settings.snapshot_dir`` doesn't exist, the scan creates it.

    Pins the ``mkdir(parents=True, exist_ok=True)`` contract in
    ``_make_snapshot_writer``: a fresh deploy where the volume mount
    is empty must NOT crash on first scan.
    """
    # Replace snapshot_dir with a path that does NOT exist.
    fresh_dir = tmp_path / "definitely-fresh"
    assert not fresh_dir.exists()
    # Settings is frozen; build a fresh instance with the new dir.
    settings = Settings(
        telegram_bot_token=settings.telegram_bot_token,
        allowed_user_ids=settings.allowed_user_ids,
        scan_time=settings.scan_time,
        tz=settings.tz,
        db_path=settings.db_path,
        snapshot_dir=fresh_dir,
        log_level=settings.log_level,
        scrape_sleep_seconds=settings.scrape_sleep_seconds,
    )

    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")
    _patch_crawl_district(monkeypatch, {31: [_snap(kurs_id=8000, availability=">2")]})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    from datetime import datetime as _dt

    today = _dt.now(settings.tz).date().isoformat()
    day_dir = fresh_dir / today
    assert day_dir.exists(), f"scan must create the date-dir under fresh_dir; got {day_dir!r}"
    # And it actually has the written file.
    files = list(day_dir.iterdir())
    assert files, "today's directory must contain the captured-page file"


async def test_classify_still_full_path_upserts_without_notification(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``"still_full"`` (belegt -> belegt) refreshes last_seen_at, no notify.

    Pins that the still_full branch:
      - DOES update ``last_seen_at``
      - Does NOT dispatch a message
      - Does NOT clobber ``last_notified_at`` back to NULL
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Seed: course previously seen as "belegt" with a non-NULL last_notified_at
    # (simulating a prior back_in_stock notification that has since lapsed).
    db.upsert_seen_course(
        conn, _snap(kurs_id=9500, title="Yoga sanft", availability="belegt"), notified=True
    )
    before = db.get_seen_course(conn, kurs_id=9500)
    assert before is not None
    assert before.last_notified_at is not None
    original_notified = before.last_notified_at
    # Backdate last_seen_at so we can prove scan touched it.
    conn.execute(
        "UPDATE seen_courses SET last_seen_at = datetime('now', '-1 minute') WHERE kurs_id = 9500"
    )
    conn.commit()
    backdated = db.get_seen_course(conn, kurs_id=9500)
    assert backdated is not None
    backdated_last_seen = backdated.last_seen_at

    # Crawl returns the same course, still "belegt" -> still_full path.
    _patch_crawl_district(
        monkeypatch, {31: [_snap(kurs_id=9500, title="Yoga sanft", availability="belegt")]}
    )
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # No notification fired.
    ctx.bot.send_message.assert_not_called()
    after = db.get_seen_course(conn, kurs_id=9500)
    assert after is not None
    # last_seen_at advanced.
    assert after.last_seen_at > backdated_last_seen, "still_full branch must refresh last_seen_at"
    # last_notified_at preserved (not clobbered to NULL).
    assert after.last_notified_at == original_notified, (
        "still_full branch must NOT overwrite last_notified_at"
    )


# ---------------------------------------------------------------------------
# Phase 6 review-pass BLOCKER: daily_scan must not notify on "new + belegt"
# ---------------------------------------------------------------------------


async def test_daily_scan_skips_new_belegt_courses(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new course whose availability is ``"belegt"`` must NOT notify.

    The locked design row in ``tasks/todo .md`` line 10 reads
    "Strict + back-in-stock: new+bookable OR full->bookable". The
    ``_run_backfill`` path already enforces the ``BOOKABLE_AVAILABILITY``
    half. Until this fix, ``daily_scan`` did not — it fired on every
    ``classify -> "new"``, including ``belegt`` first sightings. We pin
    here that:

      1. zero messages dispatch for a new-but-full course, AND
      2. the course IS upserted into ``seen_courses`` with
         ``last_notified_at = NULL``, so the next scan can correctly
         classify the belegt->bookable transition as ``"back_in_stock"``.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # Brand-new course — no prior seen_courses row — but availability is "belegt".
    snapshots = [_snap(kurs_id=11000, title="Yoga sanft", availability="belegt")]
    _patch_crawl_district(monkeypatch, {31: snapshots})
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # 1. No notification sent.
    ctx.bot.send_message.assert_not_called()

    # 2. State persisted so the NEXT scan can classify a belegt -> bookable
    #    transition as "back_in_stock" rather than re-classifying as "new".
    seen = db.get_seen_course(conn, kurs_id=11000)
    assert seen is not None, (
        "new+belegt course MUST still be upserted into seen_courses; otherwise "
        "the next scan would re-classify it as 'new' instead of 'back_in_stock'."
    )
    assert seen.last_availability == "belegt"
    assert seen.last_notified_at is None, "no notification fired -> last_notified_at stays NULL"


async def test_daily_scan_new_belegt_then_back_in_stock_chain(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full multi-hop chain that the CRITICAL INVARIANT comment promises.

    Pinned at jobs.py:323-329: "even though we skip the send here, we
    MUST still upsert the course into ``seen_courses`` with
    ``notified=False``. Otherwise the next scan ... would have no prior
    state and ``classify()`` would return ``"new"`` AGAIN instead of the
    correct ``"back_in_stock"``."

    This test exercises the whole loop end-to-end:

    1. Scan 1: snapshot = belegt, user subscribed with matching keyword.
       Assert 0 sends, ``seen_courses.last_availability == "belegt"``,
       ``last_notified_at IS NULL``.
    2. Scan 2: same ``kurs_id``, snapshot flipped to ``">2"``. Assert 1
       send, ``last_availability == ">2"``, ``last_notified_at IS NOT
       NULL``, ``notification_log`` row with ``reason="back_in_stock"``.

    If the new+belegt skip path forgot to call ``upsert_seen_course``,
    scan 2 would re-classify the course as ``"new"`` (no prior row); the
    fix in jobs.py:330-333 ensures the prior row is there so classify
    correctly returns ``"back_in_stock"``.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    # --- Scan 1: brand-new course, availability="belegt" ---------------------
    snapshots_1 = [_snap(kurs_id=12000, title="Yoga sanft", availability="belegt")]
    _patch_crawl_district(monkeypatch, {31: snapshots_1})
    ctx_1 = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx_1)

    # No dispatch on scan 1 (new+belegt is silent per the locked policy).
    ctx_1.bot.send_message.assert_not_called()
    seen_after_1 = db.get_seen_course(conn, kurs_id=12000)
    assert seen_after_1 is not None, (
        "CRITICAL INVARIANT: new+belegt must still be upserted into seen_courses"
    )
    assert seen_after_1.last_availability == "belegt"
    assert seen_after_1.last_notified_at is None
    # No notification_log row either.
    log_after_1 = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
    assert log_after_1 == 0

    # --- Scan 2: same kurs_id, availability flipped to ">2" ------------------
    snapshots_2 = [_snap(kurs_id=12000, title="Yoga sanft", availability=">2")]
    _patch_crawl_district(monkeypatch, {31: snapshots_2})
    ctx_2 = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx_2)

    # Exactly one dispatch — classified as back_in_stock.
    assert ctx_2.bot.send_message.await_count == 1, (
        f"scan 2 must classify the belegt->bookable transition as 'back_in_stock' "
        f"and dispatch exactly one message; got "
        f"{ctx_2.bot.send_message.await_count}. If this is 0, the new+belegt "
        f"skip path in jobs.py is failing to upsert the prior row."
    )
    sent_text = ctx_2.bot.send_message.await_args.kwargs["text"]
    assert "Back in stock" in sent_text

    seen_after_2 = db.get_seen_course(conn, kurs_id=12000)
    assert seen_after_2 is not None
    assert seen_after_2.last_availability == ">2"
    assert seen_after_2.last_notified_at is not None, (
        "scan 2 dispatched -> last_notified_at must be set"
    )

    # notification_log: exactly one row with reason="back_in_stock".
    rows = conn.execute("SELECT user_id, kurs_id, reason FROM notification_log").fetchall()
    assert [(r["user_id"], r["kurs_id"], r["reason"]) for r in rows] == [
        (111, 12000, "back_in_stock")
    ]


async def test_daily_scan_and_backfill_symmetric_on_new_belegt(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``daily_scan`` and ``_run_backfill`` must make the same decision for a
    brand-new ``belegt`` course: neither sends a message, both upsert the
    course into ``seen_courses`` with ``notified=False``.

    Before the BLOCKER fix, ``daily_scan`` notified and ``_run_backfill``
    skipped — surfacing the asymmetry called out in the Phase 6 finding.
    We exercise BOTH paths against the same snapshot in the same test and
    assert that ``ctx.bot.send_message`` was called the same number of
    times by both.
    """
    from unittest.mock import AsyncMock, MagicMock

    from vhsbot import handlers

    snapshots = [_snap(kurs_id=11500, title="Yoga sanft", availability="belegt")]

    # ---- daily_scan branch --------------------------------------------------
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    _patch_crawl_district(monkeypatch, {31: snapshots})
    daily_ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(daily_ctx)

    daily_send_count = daily_ctx.bot.send_message.await_count
    daily_ctx.bot.send_message.assert_not_called()
    daily_seen = db.get_seen_course(conn, kurs_id=11500)
    assert daily_seen is not None
    assert daily_seen.last_availability == "belegt"
    assert daily_seen.last_notified_at is None

    # ---- _run_backfill branch -----------------------------------------------
    # Same snapshot shape, fed through the actual backfill path so we
    # observe the real filter (not a re-derivation of BOOKABLE_AVAILABILITY
    # in the test). _run_backfill calls scraper.crawl (NOT crawl_district),
    # so we patch the public crawl entry point.
    async def fake_crawl(
        *, client: object, district_ids: object, sleep_seconds: float, keyword: str = ""
    ) -> CrawlResult:
        return CrawlResult(snapshots=tuple(snapshots), truncated=False)

    monkeypatch.setattr(handlers.scraper, "crawl", fake_crawl)

    # _run_backfill needs an update with a message we can capture replies on.
    update = MagicMock()
    update.effective_user.id = 111
    update.effective_chat.id = 111
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    backfill_ctx = _make_context(settings=settings, conn=conn, client=object())

    sent_count = await handlers._run_backfill(
        update=update, context=backfill_ctx, user_id=111, keyword="Yoga"
    )

    backfill_send_count = backfill_ctx.bot.send_message.await_count
    assert sent_count == 0, "backfill must report 0 sends for a belegt-only snapshot"
    backfill_ctx.bot.send_message.assert_not_called()

    # ---- symmetry assertion -------------------------------------------------
    assert daily_send_count == backfill_send_count, (
        "daily_scan and _run_backfill must dispatch the same number of messages "
        f"for a new+belegt course; daily={daily_send_count!r}, "
        f"backfill={backfill_send_count!r}"
    )


# ---------------------------------------------------------------------------
# Phase 7 scraper fix: daily_scan logs truncation per district
# ---------------------------------------------------------------------------


async def test_daily_scan_logs_warning_when_district_truncates(
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A district whose crawl hits the page guard must log a WARNING that
    names the district id, so an operator can see the silent under-count
    in the daily scan path. The fan-out itself must still happen for
    the snapshots we DID see — truncation never aborts the scan.

    Critical: the warning must include the district id (raw int) so an
    operator who sees the line in logs knows WHICH district truncated,
    not just "some district somewhere".
    """
    import logging as _logging

    db.upsert_user_settings(conn, user_id=111, districts=[31, 32])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    snap_d31 = _snap(kurs_id=8800, title="Yoga sanft", availability=">2", district="Mitte")

    async def per_district(
        *,
        client: object,
        district_checkbox_index: int,
        sleep_seconds: float,
        district_id: int | None = None,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> CrawlResult:
        # District 31 truncated; district 32 clean.
        if district_id == 31:
            return CrawlResult(snapshots=(snap_d31,), truncated=True)
        return CrawlResult(snapshots=(), truncated=False)

    monkeypatch.setattr(jobs.scraper, "crawl_district", per_district)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5, 32: 2}

    # ``raising=False`` was removed: ``_fetch_district_map`` exists in
    # ``jobs.py``, so the monkeypatch should fail loudly if a future rename
    # breaks the seam rather than silently no-op.
    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map)

    ctx = _make_context(settings=settings, conn=conn)

    with caplog.at_level(_logging.WARNING, logger="vhsbot.jobs"):
        await jobs.daily_scan(ctx)

    truncation_warnings = [
        rec
        for rec in caplog.records
        if "truncated" in rec.message and rec.levelno >= _logging.WARNING
    ]
    assert truncation_warnings, (
        "a truncated district must produce at least one WARNING-level log line; "
        f"records were {[(r.levelname, r.message) for r in caplog.records]!r}"
    )
    # The warning must identify WHICH district truncated — operators
    # reading the log can't act on "some district truncated".
    rendered = " ".join(rec.getMessage() for rec in truncation_warnings)
    assert "31" in rendered, (
        f"truncation warning must include the district id (31); got {rendered!r}"
    )

    # And the scan still dispatched the snapshot we DID see.
    assert ctx.bot.send_message.await_count == 1, (
        "truncation never aborts the scan; snapshots already collected must still "
        f"fan out as normal — got {ctx.bot.send_message.await_count} sends"
    )


# ---------------------------------------------------------------------------
# Round 2: pin the daily_scan -> crawl_district keyword-asymmetry contract
# ---------------------------------------------------------------------------


async def test_daily_scan_does_not_pass_keyword_to_crawl_district(
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the asymmetry: daily_scan MUST NOT forward a keyword to crawl_district.

    Background (see the ``# NB:`` comment in ``jobs.py`` next to the
    ``crawl_district`` call): the server-side ``txtSearchTerm`` filter can
    only narrow on ONE term, so passing one user's keyword would silently
    drop the other users' matches from the same shared crawl. The bot's
    client-side ``matching.matches()`` is the source of truth for
    multi-user fan-out.

    This test stubs ``crawl_district`` with a probe that fails loudly if
    ``keyword=`` is ever passed in. A future refactor that "unifies" the
    two crawl call sites by threading a keyword through daily_scan would
    trip this test before it ships.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    observed_kwargs: list[dict[str, object]] = []

    async def assert_no_keyword(**kwargs: object) -> CrawlResult:
        observed_kwargs.append(kwargs)
        # Fail loudly inside the stub so the failure mode is visible
        # whether or not the caller checks the kwargs list afterwards.
        assert kwargs.get("keyword", "") == "", (
            f"daily_scan must not pass keyword= to crawl_district; got {kwargs.get('keyword')!r}"
        )
        return CrawlResult(snapshots=(), truncated=False)

    monkeypatch.setattr(jobs.scraper, "crawl_district", assert_no_keyword)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map)

    ctx = _make_context(settings=settings, conn=conn)
    await jobs.daily_scan(ctx)

    # Sanity: the stub WAS reached (otherwise the assertion above would
    # never have run and the test would pass vacuously).
    assert observed_kwargs, "crawl_district stub must have been called"


# ---------------------------------------------------------------------------
# Phase 9: scan_log + scan_running discipline
# ---------------------------------------------------------------------------


async def test_daily_scan_writes_scan_log_row_on_success(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful scan persists exactly one ``scan_log`` row with succeeded=True."""
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    snapshots = [_snap(kurs_id=9001, title="Yoga sanft", availability=">2")]
    _patch_crawl(monkeypatch, snapshots)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.succeeded is True
    assert entry.error_summary is None
    assert entry.districts_crawled == 1
    assert entry.courses_seen >= 1
    assert entry.matches_sent == 1


async def test_daily_scan_empty_union_still_writes_scan_log(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """No active users -> scan skips crawl but STILL writes a scan_log row.

    Pin for the "I want to know if it ran" use case: an empty-union scan
    is still observable through ``/status``. All counters are zero,
    succeeded=True, error_summary is NULL.
    """
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.succeeded is True
    assert entry.error_summary is None
    assert entry.districts_crawled == 0
    assert entry.courses_seen == 0
    assert entry.matches_sent == 0


async def test_daily_scan_partial_district_failure_writes_failed_scan_log(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-district failure surfaces in scan_log with succeeded=False + district id in summary."""
    db.upsert_user_settings(conn, user_id=111, districts=[31, 32])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    snap_d31 = _snap(kurs_id=8000, title="Yoga sanft", availability=">2", district="Mitte")

    async def per_district(
        *,
        client: object,
        district_checkbox_index: int,
        sleep_seconds: float,
        district_id: int | None = None,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> CrawlResult:
        if district_id == 31:
            return CrawlResult(snapshots=(snap_d31,), truncated=False)
        raise RuntimeError("district 32 exploded")

    monkeypatch.setattr(jobs.scraper, "crawl_district", per_district)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5, 32: 2}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError, match="district 32"):
        await jobs.daily_scan(ctx)

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.succeeded is False
    assert entry.error_summary is not None
    assert "32" in entry.error_summary  # district id surfaces in summary
    assert "district 32 exploded" in entry.error_summary or "RuntimeError" in entry.error_summary


async def test_daily_scan_resets_scan_running_flag_on_failure(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical fix #1: scan_running must be reset even when the scan raises.

    Without this, a failed scan locks out every subsequent ``/scan`` and
    scheduled fire until the process restarts.
    """
    from vhsbot._app_state import BD_SCAN_RUNNING

    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError("VHS Berlin returned 503")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    # Pre-set the flag to True so we can observe the reset (and confirm
    # daily_scan does not depend on the flag being False on entry).
    ctx.bot_data[BD_SCAN_RUNNING] = True

    with pytest.raises(RuntimeError, match="503"):
        await jobs.daily_scan(ctx)

    assert ctx.bot_data[BD_SCAN_RUNNING] is False, (
        "scan_running must be reset in finally even when the scan raises"
    )


async def test_daily_scan_sets_scan_running_true_during_run_and_resets_on_success(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daily_scan owns the lifecycle of ``scan_running``: True on entry, False on exit.

    Pin for the latent-bug fix: previously only ``/scan`` set the flag, so a
    manual ``/scan`` issued during the scheduled fire could run a second
    crawl in parallel. Now the scheduled path sets the flag too.
    """
    from vhsbot._app_state import BD_SCAN_RUNNING

    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    observed_flag: list[bool] = []

    async def observe_then_succeed(**kw: Any) -> CrawlResult:
        observed_flag.append(ctx.bot_data[BD_SCAN_RUNNING])
        return CrawlResult(snapshots=(), truncated=False)

    monkeypatch.setattr(jobs.scraper, "crawl_district", observe_then_succeed)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    assert observed_flag == [True], "scan_running must be True during the crawl"
    assert ctx.bot_data[BD_SCAN_RUNNING] is False, "scan_running must be reset after the scan"


async def test_record_scan_raising_in_finally_does_not_swallow_original_exception(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical fix #2: if ``record_scan`` raises during finally, the original exception wins.

    Without the nested try/except, the record_scan exception would replace
    the original scan exception in the propagation chain (Python's finally
    semantics), and PTB's error handler would see the wrong root cause.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError("original scan failure 503")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)

    def boom_record_scan(*args: object, **kw: object) -> None:
        raise sqlite3.OperationalError("scan_log table missing somehow")

    monkeypatch.setattr(jobs, "record_scan", boom_record_scan)
    ctx = _make_context(settings=settings, conn=conn)

    # The ORIGINAL exception must be the one that propagates, not the
    # record_scan one.
    with pytest.raises(RuntimeError, match="original scan failure 503"):
        await jobs.daily_scan(ctx)


async def test_record_scan_raising_on_successful_scan_is_swallowed(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical fix #2 (corollary): a successful scan + record_scan raises -> no exception.

    The scan itself worked. ``record_scan`` is a best-effort observability
    write; if it fails, we log and continue. The caller (PTB JobQueue) sees
    a clean return.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    _patch_crawl_district(monkeypatch, {31: []})

    def boom_record_scan(*args: object, **kw: object) -> None:
        raise sqlite3.OperationalError("scan_log write failed")

    monkeypatch.setattr(jobs, "record_scan", boom_record_scan)
    ctx = _make_context(settings=settings, conn=conn)

    # Must NOT raise — scan succeeded, observability write failure is silent.
    await jobs.daily_scan(ctx)


async def test_failure_scan_log_redacts_bot_token_from_error_summary(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical fix #3: ``error_summary`` must never contain the bot token.

    Even if an exception's ``str()`` happens to embed the token (e.g.
    httpx URL formatting on a 401), the value persisted to ``scan_log``
    goes through ``Settings.redact`` before write.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    token = settings.telegram_bot_token  # 'testtoken' per the settings fixture

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError(f"Auth failed with token={token} please rotate")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError):
        await jobs.daily_scan(ctx)

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.error_summary is not None
    assert token not in entry.error_summary
    assert "[redacted]" in entry.error_summary


# ---------------------------------------------------------------------------
# Phase 9: failure-push wiring
# ---------------------------------------------------------------------------


async def test_daily_scan_failure_sends_alert_to_all_allowed_users(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed scan pushes one alert per ``settings.allowed_user_ids``.

    Note: the allowed_user_ids fixture is ``{111, 222}``; both must receive
    the alert regardless of whether they've completed onboarding.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError("scan went south")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError, match="scan went south"):
        await jobs.daily_scan(ctx)

    # Both whitelisted users must have been messaged with the failure body.
    sent_chats = {call.kwargs["chat_id"] for call in ctx.bot.send_message.await_args_list}
    assert sent_chats == settings.allowed_user_ids
    # And the body carries the exception type + a redacted detail.
    for call in ctx.bot.send_message.await_args_list:
        body = call.kwargs["text"]
        assert "RuntimeError" in body
        assert "Scan failed" in body
        assert "scan went south" in body  # the token wasn't in this str(exc)


async def test_successful_scan_does_not_send_failure_alert(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "silent on healthy completion" rule: no failure-push on success.

    Cap by counting messages that contain the failure-alert prefix. The
    daily scan still send_messages for the matched courses themselves;
    we want to ensure NO failure-alert messages.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    _patch_crawl(monkeypatch, [_snap(kurs_id=10000, title="Yoga sanft", availability=">2")])
    ctx = _make_context(settings=settings, conn=conn)

    await jobs.daily_scan(ctx)

    # No call's text body contains the failure-alert prefix.
    for call in ctx.bot.send_message.await_args_list:
        body = call.kwargs.get("text", "")
        assert "Scan failed" not in body, (
            "Healthy scan must not push a failure alert; locked design row "
            "is 'silent on healthy completion'."
        )


async def test_failure_alert_send_failure_does_not_swallow_scan_exception(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the failure-alert ``send_message`` itself raises, the original
    scan exception still propagates to PTB's error handler.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError("original scan failure 503")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    # Replace bot.send_message so EVERY call raises.
    async def boom_send(**kw: object) -> None:
        raise RuntimeError("telegram send broke")

    ctx.bot.send_message = boom_send

    # The ORIGINAL scan exception must still propagate.
    with pytest.raises(RuntimeError, match="original scan failure 503"):
        await jobs.daily_scan(ctx)


async def test_failure_alert_body_passes_through_settings_redact(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure-push body is built via ``format_failure_alert`` which redacts.

    Token in the exception's str() must never reach the Telegram message body.
    """
    db.upsert_user_settings(conn, user_id=111, districts=[31])
    db.add_subscription(conn, user_id=111, keyword="Yoga")

    token = settings.telegram_bot_token

    async def boom(**kw: Any) -> CrawlResult:
        raise RuntimeError(f"Auth failed: token={token} please rotate")

    monkeypatch.setattr(jobs.scraper, "crawl_district", boom)

    async def fake_district_map(client: object, settings: Settings) -> dict[int, int]:
        return {31: 5}

    monkeypatch.setattr(jobs, "_fetch_district_map", fake_district_map, raising=False)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError):
        await jobs.daily_scan(ctx)

    # Both whitelisted users got a message; none contain the token.
    sent_bodies = [call.kwargs["text"] for call in ctx.bot.send_message.await_args_list]
    assert sent_bodies, "failure-push must have fired"
    for body in sent_bodies:
        assert token not in body, "failure-push body must redact the bot token"
        assert "[redacted]" in body
