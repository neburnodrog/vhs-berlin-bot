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
  match the course **and** whose subscribed-districts include the
  course's district. ``"unchanged"`` and ``"still_full"`` are silent.
- **Cross-day 15/user cap**: snapshot each user's prior 24h
  ``notification_log`` count ONCE at scan start. Inside the fan-out
  loop, the cap is enforced as ``prior_count + in_scan_count >= 15``
  using only the in-scan accumulator for sends-during-this-scan — the
  prior is NOT re-queried, otherwise ``record_notification`` writes
  would double-count (each send would both insert a log row AND bump
  the in-scan counter, halving the effective budget). One INFO line per
  user when the cap is first hit.
- **Snapshot persistence**: every raw response HTML is written to
  ``<snapshot_dir>/YYYY-MM-DD/<district>-page-<N>.html`` via a callback
  passed into ``scraper.crawl_district``. Writer failures are logged
  (warning) and swallowed — snapshots are debug-only; never abort the
  scan. After the scan, directories older than 7 days are pruned. Prune
  failures are also logged + continued.
- **Storage**: every returned course is upserted into ``seen_courses``
  with a fresh ``last_seen_at``, regardless of whether we notified;
  notified ones also bump ``last_notified_at``.
- **Partial-failure recovery**: we drive ``scraper.crawl_district``
  ourselves per-district (NOT ``scraper.crawl``'s all-or-nothing
  wrapper), so a single failing district leaves the others' snapshots
  persisted in seen_courses. After all districts have been attempted,
  if any failed we re-raise the first exception so PTB's
  ``add_error_handler`` sees it.

Top-level try/except re-raises after logging so PTB's
``add_error_handler`` sees the failure (per Phase 4 fix).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
from telegram.ext import ContextTypes

from vhsbot import scraper
from vhsbot._app_state import (
    BD_CLIENT,
    BD_DB,
    BD_SCAN_RUNNING,
    BD_SETTINGS,
    locked_db,
)
from vhsbot.config import Settings
from vhsbot.db import (
    BOOKABLE_AVAILABILITY,
    CourseSnapshot,
    all_active_user_ids,
    count_notifications_since,
    get_seen_course,
    get_user_settings,
    list_subscriptions,
    record_notification,
    record_scan,
    union_active_districts,
    upsert_seen_course,
)
from vhsbot.diff import NotificationReason, classify
from vhsbot.formatting import course_card, format_failure_alert
from vhsbot.matching import matches
from vhsbot.parser import parse_district_map

logger = logging.getLogger(__name__)

# Cross-day cap on notifications per user. The locked design's
# notification policy row pins this at 15/day; the look-back window is
# the trailing 24h, not the calendar day, so two consecutive scans
# at the same wall-clock time can't accidentally double the budget.
DAILY_CAP = 15
SNAPSHOT_RETENTION_DAYS = 7


@dataclass(frozen=True, slots=True)
class _UserSubs:
    """Bundle of one user's settings + keywords. Internal helper struct.

    ``include_waitlist`` is intentionally NOT carried here — the parser's
    availability literals (``>2|2|1|belegt``) do not surface a waitlist
    state explicitly, so a per-user opt-in flag is inert today. If a
    fifth literal ever appears, restore this field. See ``tasks/todo .md``
    locked-design row "Notification policy".
    """

    user_id: int
    districts: frozenset[int]
    keywords: tuple[str, ...]


@dataclass(slots=True)
class _ScanProgress:
    """Mutable counters + first-error summary, read by ``daily_scan``'s finally.

    Passed by reference through ``_run_daily_scan`` and
    ``_process_district_snapshots`` so partial progress is observable
    when the scan raises mid-flight. The ``error_summary`` field
    captures a redacted, district-tagged summary of the FIRST per-district
    failure (later ones overwrite nothing). The finally clause uses the
    captured summary in preference to ``str(exc)`` because it carries
    the district context that the bare exception lacks.
    """

    districts_crawled: int = 0
    courses_seen: int = 0
    matches_sent: int = 0
    error_summary: str | None = field(default=None)


def _date_dir_name(d: date) -> str:
    """YYYY-MM-DD directory name used under ``snapshot_dir``."""
    return d.isoformat()


def _make_snapshot_writer(snapshot_dir: Path, today: date) -> scraper.RawHtmlCallback:
    """Build the ``raw_html_callback`` closure for one scan's worth of pages.

    Returns a sync callable matching the
    ``scraper.RawHtmlCallback`` shape. Each call writes
    ``<snapshot_dir>/YYYY-MM-DD/<district>-page-<N>.html``. Write
    failures are logged at WARNING and swallowed — snapshots are debug
    artefacts, never load-bearing.
    """
    target_dir = snapshot_dir / _date_dir_name(today)

    def _write(district_id: int, page_idx: int, content: bytes) -> None:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{district_id}-page-{page_idx}.html"
            path.write_bytes(content)
        except OSError as e:
            logger.warning("snapshot write failed: %s", e)

    return _write


def _prune_old_snapshots(snapshot_dir: Path, today: date) -> None:
    """Delete date-named directories older than ``SNAPSHOT_RETENTION_DAYS``.

    Anything in ``snapshot_dir`` whose name does not parse as ISO date
    is left alone (defensive: never delete a directory we didn't create).
    Boundary: a directory exactly ``today - 7d`` old IS pruned (weekly
    cleanup semantics — keep 7 days of snapshots, drop the 8th).
    Permission/OS errors on rmtree are logged and skipped; one stuck
    directory must not prevent the rest of the scan from finishing.
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
        if parsed <= cutoff:
            try:
                shutil.rmtree(child)
            except OSError as e:
                logger.warning("snapshot prune failed for %s: %s", child, e)
                continue


def _since_24h_iso(now: datetime) -> str:
    return (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


async def _fetch_district_map(client: httpx.AsyncClient, settings: Settings) -> dict[int, int]:
    """Fetch ``CourseSearch.aspx`` once and parse the district checkbox map.

    Distinct from ``handlers._fetch_district_map`` so the daily-scan
    path can be monkeypatched in tests without touching the handler
    seam. Both call sites hit the same URL and parse the same form.
    """
    resp = await client.get(settings.search_url)
    resp.raise_for_status()
    return parse_district_map(resp.content)


def _summarize_exc(exc: BaseException, settings: Settings) -> str:
    """Build a single-line, token-redacted summary of an exception for ``scan_log``."""
    return f"{type(exc).__name__}: {settings.redact(str(exc))[:200]}"


async def _send_failure_alerts(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    exc: BaseException,
) -> None:
    """Push a single-line redacted failure alert to every whitelisted user.

    "Allowed users" (``settings.allowed_user_ids``) is the operator set
    for this single-user bot — even before any user has run ``/start``,
    the operator should hear about scan failures immediately. Iterating
    in sorted order keeps log lines deterministic; a single failed
    recipient must NOT block the others from being notified.
    """
    when = datetime.now(settings.tz)
    text = format_failure_alert(exc, settings, when)
    for user_id in sorted(settings.allowed_user_ids):
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception:
            logger.exception("failure_alert send to user %s failed", user_id)


async def daily_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The JobQueue daily callback. See module docstring for the full spec.

    Phase 9 wraps the body in a try/finally that does THREE things on exit
    (in this exact order — see ``tasks/todo .md`` "Critical fixes"):

    1. **Nested try/except** writes a ``scan_log`` row. A ``record_scan``
       failure is logged and swallowed so it can never displace the
       original scan exception in the propagation chain (Python's
       finally semantics replace exceptions otherwise).
    2. Reset ``bot_data[BD_SCAN_RUNNING] = False`` so a transient crash
       does not strand the bot in a locked-out state (the next ``/scan``
       or scheduled fire would silently defer forever otherwise).
    3. The outer ``try``'s ``raise`` propagates as the finally exits,
       so PTB's ``add_error_handler`` still sees the failure.

    The ``BD_SCAN_RUNNING`` flag is owned by BOTH the manual ``/scan``
    handler (handlers.scan) and this scheduled job; either side setting
    the flag blocks the other, so the two paths never run in parallel.
    """
    settings: Settings = context.bot_data[BD_SETTINGS]
    progress = _ScanProgress()
    exc: BaseException | None = None
    context.bot_data[BD_SCAN_RUNNING] = True
    try:
        await _run_daily_scan(context, progress)
    except Exception as e:
        exc = e
        logger.exception("daily_scan failed")
        # Failure-push: notify every whitelisted user that the scan
        # failed. Wrapped in nested try/except so a send failure cannot
        # mask the original scan exception in the propagation chain.
        try:
            await _send_failure_alerts(context, settings, e)
        except Exception:
            logger.exception("failure_alert send failed (swallowed)")
        raise
    finally:
        # (1) Persist the scan_log row. Nested try/except so any failure
        # in this best-effort observability write is logged + swallowed,
        # never propagated (would otherwise mask the real exception).
        try:
            if exc is None:
                succeeded = True
                err: str | None = None
            else:
                succeeded = False
                # Prefer the district-tagged summary captured at the
                # per-district catch site; fall back to a redacted top-
                # level summary if the exception happened outside the
                # per-district loop (e.g. ``_fetch_district_map`` raised).
                err = progress.error_summary or _summarize_exc(exc, settings)
            async with locked_db(context) as conn:
                record_scan(
                    conn,
                    districts_crawled=progress.districts_crawled,
                    courses_seen=progress.courses_seen,
                    matches_sent=progress.matches_sent,
                    succeeded=succeeded,
                    error_summary=err,
                )
        except Exception:
            logger.exception("scan_log write failed (swallowed)")
        # (2) Reset the concurrency flag so future scans can run.
        context.bot_data[BD_SCAN_RUNNING] = False
        # (3) The outer try's ``raise`` (if any) propagates as we exit.


async def _run_daily_scan(context: ContextTypes.DEFAULT_TYPE, progress: _ScanProgress) -> None:
    settings: Settings = context.bot_data[BD_SETTINGS]
    client: httpx.AsyncClient = context.bot_data[BD_CLIENT]
    conn: sqlite3.Connection = context.bot_data[BD_DB]

    today = datetime.now(settings.tz).date()
    since_24h_iso = _since_24h_iso(datetime.now(UTC))

    # 1. Collect target districts + active-user state.
    async with locked_db(context):
        districts = union_active_districts(conn)
        active_user_subs = _load_active_user_subs(conn)
        # BLOCKER-1 fix: snapshot prior counts ONCE per user up front.
        # Re-querying after each record_notification would double-count
        # the in-scan sends.
        prior_counts: dict[int, int] = {
            sub.user_id: count_notifications_since(conn, user_id=sub.user_id, since=since_24h_iso)
            for sub in active_user_subs.values()
        }

    if not districts:
        logger.info("daily_scan: no active users -> skipping crawl")
        _prune_old_snapshots(settings.snapshot_dir, today)
        return

    # 2. Resolve district_id -> checkbox_index once, before the per-district loop.
    district_map = await _fetch_district_map(client, settings)
    unknown = sorted(d for d in districts if d not in district_map)
    if unknown:
        raise ValueError(f"unknown district id(s): {unknown}; known: {sorted(district_map)}")

    writer = _make_snapshot_writer(settings.snapshot_dir, today)

    # 3-6: per-district crawl + classify + fan-out + cap + send + log + upsert.
    #
    # MAJOR-6 fix: each district is wrapped in try/except so a single
    # failing district doesn't strand the rest of the run (otherwise the
    # other districts' seen_courses rows would not get refreshed and
    # tomorrow they'd re-notify as "new").
    sent_per_user: dict[int, int] = dict.fromkeys(active_user_subs, 0)
    cap_warned: set[int] = set()
    first_error: Exception | None = None
    total_snapshots = 0
    seen_kurs_ids: set[int] = set()

    for district_id in sorted(districts):
        checkbox_index = district_map[district_id]
        try:
            # NB: deliberately no ``keyword=`` here — daily_scan fans out
            # N user keywords across one shared crawl; the server-side
            # filter (``txtSearchTerm``) can only narrow on a single term
            # and cannot narrow a multi-keyword union, so passing one
            # user's keyword would silently filter out the matches the
            # OTHER users were waiting for. The bot's client-side
            # ``matching.matches()`` handles filtering downstream.
            # ``_run_backfill``'s single-keyword path IS allowed to pass
            # ``keyword=`` because it serves exactly one user/keyword pair.
            district_result = await scraper.crawl_district(
                client=client,
                district_checkbox_index=checkbox_index,
                sleep_seconds=settings.scrape_sleep_seconds,
                district_id=district_id,
                raw_html_callback=writer,
            )
        except Exception as e:
            logger.exception("crawl_district failed for district %s", district_id)
            if first_error is None:
                first_error = e
                # Capture a district-tagged summary so the scan_log row
                # carries enough context to diagnose without the
                # traceback. Redact via Settings so a hypothetical
                # token-leaking exception never lands in storage.
                progress.error_summary = (
                    f"district {district_id}: {type(e).__name__}: {settings.redact(str(e))[:200]}"
                )
            continue

        progress.districts_crawled += 1

        # Truncation is a silent under-count: the scan correctly persisted
        # everything it saw, but the tail of this district's result set
        # was dropped because we hit ``MAX_PAGES_GUARD``. Surface it at
        # WARNING so the operator notices, but do NOT abort or notify the
        # user from the daily-scan path — the snapshots we DID see are
        # still classified + dispatched as normal.
        if district_result.truncated:
            logger.warning(
                "daily_scan: district %s crawl truncated at page guard %s; "
                "snapshots from later pages dropped",
                district_id,
                scraper.MAX_PAGES_GUARD,
            )

        # Dedup across districts (a course may legitimately appear in
        # several due to "Alle Bezirke"-style cross-tags); first-occurrence
        # wins, matching the all-districts ``scraper.crawl`` semantics.
        deduped: list[CourseSnapshot] = []
        for snap in district_result.snapshots:
            if snap.kurs_id in seen_kurs_ids:
                continue
            seen_kurs_ids.add(snap.kurs_id)
            deduped.append(snap)

        total_snapshots += len(deduped)
        progress.courses_seen += len(deduped)
        await _process_district_snapshots(
            context=context,
            conn=conn,
            settings=settings,
            district_id=district_id,
            snapshots=deduped,
            active_user_subs=active_user_subs,
            prior_counts=prior_counts,
            sent_per_user=sent_per_user,
            cap_warned=cap_warned,
            progress=progress,
        )

    # 7. Snapshot persistence already happened inline (via writer).
    # 8. Pruning runs at the end so a crashed mid-scan still keeps today's writes.
    _prune_old_snapshots(settings.snapshot_dir, today)
    logger.info(
        "daily_scan complete: %s snapshots, %s notifications",
        total_snapshots,
        sum(sent_per_user.values()),
    )

    if first_error is not None:
        # Re-raise the first per-district failure so PTB's error handler
        # sees it — but only AFTER every other district's state has been
        # persisted.
        raise first_error


async def _process_district_snapshots(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    conn: sqlite3.Connection,
    settings: Settings,
    district_id: int,
    snapshots: list[CourseSnapshot],
    active_user_subs: dict[int, _UserSubs],
    prior_counts: dict[int, int],
    sent_per_user: dict[int, int],
    cap_warned: set[int],
    progress: _ScanProgress,
) -> None:
    """Classify, fan-out, log, and upsert the snapshots from one district.

    Factored out of ``_run_daily_scan`` so the per-district loop stays
    readable. Mutates ``sent_per_user`` + ``cap_warned`` in-place.
    """
    for snap in snapshots:
        async with locked_db(context):
            previous = get_seen_course(conn, kurs_id=snap.kurs_id)
        result = classify(snap, previous)

        if result not in ("new", "back_in_stock"):
            # Refresh last_seen_at so we observe staleness later.
            async with locked_db(context):
                upsert_seen_course(conn, snap, notified=False)
            continue

        # Phase 6 review BLOCKER fix: enforce "new+bookable" symmetrically
        # with ``handlers._run_backfill``. The locked-design notification
        # policy row reads "new+bookable OR full->bookable" — until now,
        # ``daily_scan`` fired on every ``classify -> "new"`` regardless of
        # availability, including ``belegt`` first sightings (asymmetric
        # with the backfill path's ``BOOKABLE_AVAILABILITY`` filter).
        #
        # CRITICAL INVARIANT: even though we skip the send here, we MUST
        # still upsert the course into ``seen_courses`` with
        # ``notified=False``. Otherwise the next scan — where the same
        # course flips to bookable — would have no prior state and
        # ``classify()`` would return ``"new"`` AGAIN instead of the
        # correct ``"back_in_stock"``, and the user would never hear
        # about the cancellation that made the course available.
        if result == "new" and snap.availability not in BOOKABLE_AVAILABILITY:
            async with locked_db(context):
                upsert_seen_course(conn, snap, notified=False)
            continue
        # The Literal guard above narrows ``result`` to NotificationReason.
        narrowed_result = cast(NotificationReason, result)

        any_user_notified = False
        for user_id, sub in active_user_subs.items():
            # MAJOR-2: per-user district filter. A user subscribed to
            # district 31 only must never receive a district 32 course
            # just because some other user pulled district 32 into the
            # union.
            if district_id not in sub.districts:
                continue
            matched_keywords = matches(snap, sub.keywords)
            if not matched_keywords:
                continue

            # BLOCKER-1: cap = prior (snapshotted once) + in-scan.
            if prior_counts.get(user_id, 0) + sent_per_user.get(user_id, 0) >= DAILY_CAP:
                if user_id not in cap_warned:
                    logger.info("user %s reached daily cap (%s)", user_id, DAILY_CAP)
                    cap_warned.add(user_id)
                continue

            detail_url = settings.detail_url_template.format(kurs_id=snap.kurs_id)
            text, markup = course_card(snap, matched_keywords, narrowed_result, detail_url)
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=markup,
            )
            async with locked_db(context):
                record_notification(
                    conn, user_id=user_id, kurs_id=snap.kurs_id, reason=narrowed_result
                )
            sent_per_user[user_id] = sent_per_user.get(user_id, 0) + 1
            progress.matches_sent += 1
            any_user_notified = True

        # Upsert seen_courses for this snapshot, marking notified iff any
        # user got a message for it (drives last_notified_at).
        async with locked_db(context):
            upsert_seen_course(conn, snap, notified=any_user_notified)


def _load_active_user_subs(conn: sqlite3.Connection) -> dict[int, _UserSubs]:
    """Load every active (non-paused) user's settings + keywords.

    Caller must hold the DB lock.
    """
    out: dict[int, _UserSubs] = {}
    for user_id in all_active_user_ids(conn):
        user_settings = get_user_settings(conn, user_id=user_id)
        if user_settings is None:
            continue
        # waitlist is implicit in the parser's availability literals
        # (>2|2|1|belegt); include_waitlist is read in storage but inert
        # today — when waitlist becomes explicit, restore this field.
        keywords = list_subscriptions(conn, user_id=user_id)
        out[user_id] = _UserSubs(
            user_id=user_settings.user_id,
            districts=user_settings.districts,
            keywords=tuple(keywords),
        )
    return out


__all__ = ["DAILY_CAP", "SNAPSHOT_RETENTION_DAYS", "daily_scan"]
