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
from collections.abc import Callable, Iterable
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
    """Replace ``scraper.crawl`` so daily_scan returns ``snapshots``.

    If ``capture_callback`` is provided, the substitute also invokes the
    callback once per snapshot with synthetic ``(district, page, html)``
    triples so the snapshot-writer side of ``daily_scan`` can be exercised
    without going through the real scraper.
    """

    async def fake_crawl(
        *,
        client: object,
        district_ids: Iterable[int],
        sleep_seconds: float,
        raw_html_callback: Callable[[int, int, bytes], None] | None = None,
    ) -> list[CourseSnapshot]:
        if capture_callback is not None and raw_html_callback is not None:
            # Synthesize one (district, page0) call per district.
            for d in sorted(set(district_ids)):
                html = b"<html>page-0 for district " + str(d).encode() + b"</html>"
                raw_html_callback(d, 0, html)
                capture_callback.append((d, 0, b"<html>"))
        elif raw_html_callback is not None:
            # Always call once per requested district even without capture.
            for d in sorted(set(district_ids)):
                raw_html_callback(d, 0, b"<html>page-0</html>")
        return snapshots

    monkeypatch.setattr(jobs.scraper, "crawl", fake_crawl)


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

    monkeypatch.setattr(jobs.scraper, "crawl", boom_crawl)
    ctx = _make_context(settings=settings, conn=conn)

    with pytest.raises(RuntimeError, match="503"):
        await jobs.daily_scan(ctx)
