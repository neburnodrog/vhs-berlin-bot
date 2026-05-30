"""Daily-scan JobQueue callback for vhs-berlin-bot.

Pure orchestration: the heavy lifting (HTTP, parsing, matching, diffing,
formatting, storage) all lives in the modules below. This module wires
them together once per day.

The locked design pinned by ``tasks/todo .md``:

- **Crawl scope**: union of all *active* (non-paused) whitelisted users'
  districts. Empty union -> skip the scan entirely (no point waking the
  site for zero subscribers).
- **Notification policy**: notify on ``"new"`` and ``"back_in_stock"``
  classifications only, fanned out to every active user whose keywords
  match the course. ``"unchanged"`` and ``"still_full"`` are silent.
- **Cross-day 15/user cap**: before each send, count this user's
  ``notification_log`` rows in the last 24h and skip once the total
  (including sends-in-this-scan) reaches 15. Log a single INFO line when
  the cap is first hit.
- **Snapshot persistence**: every raw response HTML is written to
  ``<snapshot_dir>/YYYY-MM-DD/<district>-page-<N>.html`` via a callback
  passed into ``scraper.crawl``. After the scan, directories older than
  7 days are pruned.
- **Storage**: every returned course is upserted into ``seen_courses``
  with a fresh ``last_seen_at``, regardless of whether we notified;
  notified ones also bump ``last_notified_at``.

Top-level try/except re-raises after logging so PTB's
``add_error_handler`` sees the failure (per Phase 4 fix).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from telegram.ext import ContextTypes

from vhsbot import scraper
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.db import (
    CourseSnapshot,
    UserSettings,
    count_notifications_since,
    get_seen_course,
    get_user_settings,
    list_subscriptions,
    record_notification,
    union_active_districts,
    upsert_seen_course,
)
from vhsbot.diff import classify
from vhsbot.formatting import course_card
from vhsbot.matching import matches

logger = logging.getLogger(__name__)

# Cross-day cap on notifications per user. The locked design's
# notification policy row pins this at 15/day; the look-back window is
# the trailing 24h, not the calendar day, so two consecutive scans
# at the same wall-clock time can't accidentally double the budget.
DAILY_CAP = 15
SNAPSHOT_RETENTION_DAYS = 7


def _date_dir_name(d: date) -> str:
    """YYYY-MM-DD directory name used under ``snapshot_dir``."""
    return d.isoformat()


def _make_snapshot_writer(snapshot_dir: Path, today: date) -> Any:
    """Build the ``raw_html_callback`` closure for one scan's worth of pages.

    Returns a sync callable matching the
    ``scraper.RawHtmlCallback`` shape. Each call writes
    ``<snapshot_dir>/YYYY-MM-DD/<district>-page-<N>.html``.
    """
    target_dir = snapshot_dir / _date_dir_name(today)

    def _write(district_id: int, page_idx: int, content: bytes) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{district_id}-page-{page_idx}.html"
        path.write_bytes(content)

    return _write


def _prune_old_snapshots(snapshot_dir: Path, today: date) -> None:
    """Delete date-named directories older than ``SNAPSHOT_RETENTION_DAYS``.

    Anything in ``snapshot_dir`` whose name does not parse as ISO date
    is left alone (defensive: never delete a directory we didn't create).
    """
    if not snapshot_dir.exists():
        return
    cutoff = today - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    for child in snapshot_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            parsed = date.fromisoformat(child.name)
        except ValueError:
            continue
        if parsed < cutoff:
            _rmtree(child)


def _rmtree(p: Path) -> None:
    """Minimal recursive directory delete. Avoids importing shutil for this."""
    for entry in p.iterdir():
        if entry.is_dir():
            _rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
    p.rmdir()


def _utc_now_iso() -> str:
    """ISO-formatted UTC timestamp matching sqlite's ``datetime('now')``."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _since_24h_iso() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


async def daily_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The JobQueue daily callback. See module docstring for the full spec."""
    try:
        await _run_daily_scan(context)
    except Exception:
        logger.exception("daily_scan failed")
        raise


async def _run_daily_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data[BD_SETTINGS]
    client: httpx.AsyncClient = context.bot_data[BD_CLIENT]
    conn = context.bot_data[BD_DB]
    lock = context.bot_data[BD_DB_LOCK]

    today = datetime.now(settings.tz).date()

    # 1. Collect target districts (union over all active users).
    async with lock:
        districts = union_active_districts(conn)
        active_user_subs = _load_active_user_subs(conn)

    if not districts:
        logger.info("daily_scan: no active users -> skipping crawl")
        _prune_old_snapshots(settings.snapshot_dir, today)
        return

    # 2. Crawl with snapshot persistence.
    writer = _make_snapshot_writer(settings.snapshot_dir, today)
    snapshots = await scraper.crawl(
        client=client,
        district_ids=sorted(districts),
        sleep_seconds=settings.scrape_sleep_seconds,
        raw_html_callback=writer,
    )

    # 3 + 4 + 5 + 6: classify, fan out, cap, send, log, upsert.
    sent_per_user: dict[int, int] = dict.fromkeys(active_user_subs, 0)
    cap_warned: set[int] = set()

    for snap in snapshots:
        async with lock:
            previous = get_seen_course(conn, kurs_id=snap.kurs_id)
        result = classify(snap, previous)

        if result not in ("new", "back_in_stock"):
            # Refresh last_seen_at so we observe staleness later.
            async with lock:
                upsert_seen_course(conn, snap, notified=False)
            continue

        any_user_notified = False
        for user_id, sub in active_user_subs.items():
            matched_keywords = matches(snap, sub.keywords)
            if not matched_keywords:
                continue

            # Cross-day cap: existing log rows + sends-in-this-scan.
            async with lock:
                prior_count = count_notifications_since(
                    conn, user_id=user_id, since=_since_24h_iso()
                )
            in_scan = sent_per_user.get(user_id, 0)
            if prior_count + in_scan >= DAILY_CAP:
                if user_id not in cap_warned:
                    logger.info("user %s reached daily cap (%s)", user_id, DAILY_CAP)
                    cap_warned.add(user_id)
                continue

            detail_url = settings.detail_url_template.format(kurs_id=snap.kurs_id)
            text, markup = course_card(snap, matched_keywords, result, detail_url)
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=markup,
            )
            async with lock:
                record_notification(conn, user_id=user_id, kurs_id=snap.kurs_id, reason=result)
            sent_per_user[user_id] = in_scan + 1
            any_user_notified = True

        # Upsert seen_courses for this snapshot, marking notified iff any
        # user got a message for it (drives last_notified_at).
        async with lock:
            upsert_seen_course(conn, snap, notified=any_user_notified)

    # 7. Snapshot persistence already happened inline (via writer).
    # 8. Pruning runs at the end so a crashed mid-scan still keeps today's writes.
    _prune_old_snapshots(settings.snapshot_dir, today)
    logger.info(
        "daily_scan complete: %s snapshots, %s notifications",
        len(snapshots),
        sum(sent_per_user.values()),
    )


class _UserSubs:
    """Bundle of one user's settings + keywords. Internal helper struct."""

    __slots__ = ("districts", "include_waitlist", "keywords", "user_id")

    def __init__(self, settings: UserSettings, keywords: list[str]) -> None:
        self.user_id = settings.user_id
        self.districts = settings.districts
        self.keywords = keywords
        self.include_waitlist = settings.include_waitlist


def _load_active_user_subs(conn: Any) -> dict[int, _UserSubs]:
    """Load every active (non-paused) user's settings + keywords.

    Combined in one helper so the daily-scan loop reads cleanly. Caller
    must hold the DB lock.
    """
    from vhsbot.db import all_active_user_ids  # local import keeps the module DAG shallow

    out: dict[int, _UserSubs] = {}
    for user_id in all_active_user_ids(conn):
        user_settings = get_user_settings(conn, user_id=user_id)
        if user_settings is None:
            continue
        keywords = list_subscriptions(conn, user_id=user_id)
        out[user_id] = _UserSubs(user_settings, keywords)
    return out


# Re-export for tests that want to inject a different ``CourseSnapshot``
# alias without import-juggling.
__all__ = ["DAILY_CAP", "SNAPSHOT_RETENTION_DAYS", "CourseSnapshot", "daily_scan"]
