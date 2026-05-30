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
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from vhsbot import db, jobs
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.db import CourseSnapshot

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


class _AsyncContextLock:
    """Minimal async lock replacement used in handler tests.

    Re-implementing here rather than importing from test_handlers to
    keep the test module standalone.
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
    client: object | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot_data = {
        BD_SETTINGS: settings,
        BD_DB: conn,
        BD_DB_LOCK: _AsyncContextLock(),
        BD_CLIENT: client or object(),
    }
    return ctx


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
    ) -> list[CourseSnapshot]:
        assert district_id is not None
        if raw_html_callback is not None:
            html = b"<html>page-0 for district " + str(district_id).encode() + b"</html>"
            raw_html_callback(district_id, 0, html)
            if capture_callback is not None:
                capture_callback.append((district_id, 0, b"<html>"))
        # Hand the snapshots to the FIRST district visited only.
        if not delivered:
            delivered[district_id] = True
            return snapshots
        return []

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

    async def boom_crawl(**kw: Any) -> list[CourseSnapshot]:
        crawl_called.append(True)
        return []

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

    async def boom_crawl(**kw: Any) -> list[CourseSnapshot]:
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
    ) -> list[CourseSnapshot]:
        if district_id == 31:
            return [snap_d31]
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
    ) -> list[CourseSnapshot]:
        assert district_id is not None
        if raw_html_callback is not None:
            raw_html_callback(district_id, 0, b"<html>fake</html>")
        return per_district_snapshots.get(district_id, [])

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
