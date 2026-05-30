"""SQLite storage layer.

Pass an explicit ``sqlite3.Connection`` to every function so callers can
share one connection in production (guarded by an ``asyncio.Lock``) and
spin up isolated ``:memory:`` connections in tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id    INTEGER NOT NULL,
    keyword    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, keyword)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id          INTEGER PRIMARY KEY,
    districts_csv    TEXT NOT NULL,
    paused           INTEGER NOT NULL DEFAULT 0,
    include_waitlist INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seen_courses (
    kurs_id            INTEGER PRIMARY KEY,
    title              TEXT NOT NULL,
    course_number      TEXT NOT NULL,
    district           TEXT,
    venue              TEXT,
    date_range         TEXT,
    last_availability  TEXT NOT NULL,
    first_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_notified_at   TEXT
);

CREATE TABLE IF NOT EXISTS notification_log (
    user_id  INTEGER NOT NULL,
    kurs_id  INTEGER NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    reason   TEXT NOT NULL,
    PRIMARY KEY (user_id, kurs_id, reason)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA)


# --- subscriptions ----------------------------------------------------------


def add_subscription(conn: sqlite3.Connection, *, user_id: int, keyword: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, keyword) VALUES (?, ?)",
            (user_id, keyword),
        )


def remove_subscription(conn: sqlite3.Connection, *, user_id: int, keyword: str) -> bool:
    with conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND keyword = ?",
            (user_id, keyword),
        )
    return cur.rowcount > 0


def list_subscriptions(conn: sqlite3.Connection, *, user_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT keyword FROM subscriptions WHERE user_id = ? ORDER BY keyword",
        (user_id,),
    ).fetchall()
    return [row["keyword"] for row in rows]


def all_subscribed_user_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT user_id FROM subscriptions ORDER BY user_id").fetchall()
    return [row["user_id"] for row in rows]


# --- user_settings ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserSettings:
    user_id: int
    districts: frozenset[int]
    paused: bool
    include_waitlist: bool


def _districts_to_csv(districts: Iterable[int]) -> str:
    return ",".join(str(d) for d in sorted(districts))


def _districts_from_csv(csv: str) -> frozenset[int]:
    if not csv:
        return frozenset()
    return frozenset(int(p) for p in csv.split(",") if p.strip())


def upsert_user_settings(
    conn: sqlite3.Connection, *, user_id: int, districts: Iterable[int]
) -> None:
    csv = _districts_to_csv(districts)
    with conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, districts_csv)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET districts_csv = excluded.districts_csv
            """,
            (user_id, csv),
        )


def get_user_settings(conn: sqlite3.Connection, *, user_id: int) -> UserSettings | None:
    row = conn.execute(
        "SELECT user_id, districts_csv, paused, include_waitlist FROM user_settings "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return UserSettings(
        user_id=row["user_id"],
        districts=_districts_from_csv(row["districts_csv"]),
        paused=bool(row["paused"]),
        include_waitlist=bool(row["include_waitlist"]),
    )


def set_paused(conn: sqlite3.Connection, *, user_id: int, paused: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE user_settings SET paused = ? WHERE user_id = ?",
            (1 if paused else 0, user_id),
        )


def all_active_user_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT user_id FROM user_settings WHERE paused = 0 ORDER BY user_id"
    ).fetchall()
    return [row["user_id"] for row in rows]


def union_active_districts(conn: sqlite3.Connection) -> frozenset[int]:
    rows = conn.execute("SELECT districts_csv FROM user_settings WHERE paused = 0").fetchall()
    out: set[int] = set()
    for row in rows:
        out |= _districts_from_csv(row["districts_csv"])
    return frozenset(out)


# --- seen_courses ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CourseSnapshot:
    kurs_id: int
    title: str
    course_number: str
    district: str | None
    venue: str | None
    date_range: str | None
    availability: str


@dataclass(frozen=True, slots=True)
class SeenCourse:
    kurs_id: int
    title: str
    course_number: str
    district: str | None
    venue: str | None
    date_range: str | None
    last_availability: str
    first_seen_at: str
    last_seen_at: str
    last_notified_at: str | None


def mark_seen(conn: sqlite3.Connection, snapshot: CourseSnapshot) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO seen_courses (
                kurs_id, title, course_number, district, venue, date_range, last_availability
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kurs_id) DO UPDATE SET
                title             = excluded.title,
                course_number     = excluded.course_number,
                district          = excluded.district,
                venue             = excluded.venue,
                date_range        = excluded.date_range,
                last_availability = excluded.last_availability,
                last_seen_at      = datetime('now')
            """,
            (
                snapshot.kurs_id,
                snapshot.title,
                snapshot.course_number,
                snapshot.district,
                snapshot.venue,
                snapshot.date_range,
                snapshot.availability,
            ),
        )


def get_seen_course(conn: sqlite3.Connection, *, kurs_id: int) -> SeenCourse | None:
    row = conn.execute(
        "SELECT kurs_id, title, course_number, district, venue, date_range, "
        "last_availability, first_seen_at, last_seen_at, last_notified_at "
        "FROM seen_courses WHERE kurs_id = ?",
        (kurs_id,),
    ).fetchone()
    if row is None:
        return None
    return SeenCourse(
        kurs_id=row["kurs_id"],
        title=row["title"],
        course_number=row["course_number"],
        district=row["district"],
        venue=row["venue"],
        date_range=row["date_range"],
        last_availability=row["last_availability"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_notified_at=row["last_notified_at"],
    )


def get_seen_availability(conn: sqlite3.Connection, *, kurs_ids: Iterable[int]) -> dict[int, str]:
    ids = list(kurs_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT kurs_id, last_availability FROM seen_courses WHERE kurs_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {row["kurs_id"]: row["last_availability"] for row in rows}


def mark_notified(conn: sqlite3.Connection, *, kurs_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE seen_courses SET last_notified_at = datetime('now') WHERE kurs_id = ?",
            (kurs_id,),
        )


def set_last_availability(
    conn: sqlite3.Connection, *, kurs_id: int, availability: Availability
) -> None:
    """Overwrite the stored ``last_availability`` for one course.

    Test-only seam used by the e2e tests to force a belegt -> bookable
    transition on a course that the fixture reports as bookable. Lifted
    out of ad-hoc raw SQL so the type system (``Availability`` literal)
    can catch typos at call sites — a stuck typo would silently make
    classify() raise on every future scan.
    """
    with conn:
        conn.execute(
            "UPDATE seen_courses SET last_availability = ? WHERE kurs_id = ?",
            (availability, kurs_id),
        )


def upsert_seen_course(
    conn: sqlite3.Connection, snapshot: CourseSnapshot, *, notified: bool
) -> None:
    """Idempotent insert-or-update of a ``seen_courses`` row.

    Always refreshes ``last_seen_at`` to ``datetime('now')``. Refreshes
    ``last_notified_at`` only when ``notified=True``. On insert,
    ``first_seen_at`` defaults to ``datetime('now')``; on update it is
    preserved (we never overwrite the original sighting timestamp).

    This is the Phase 5 daily-scan equivalent of :func:`mark_seen` +
    optional :func:`mark_notified` in a single round-trip, with the
    ``notified`` flag picking which side fires.
    """
    if notified:
        with conn:
            conn.execute(
                """
                INSERT INTO seen_courses (
                    kurs_id, title, course_number, district, venue, date_range,
                    last_availability, last_notified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(kurs_id) DO UPDATE SET
                    title             = excluded.title,
                    course_number     = excluded.course_number,
                    district          = excluded.district,
                    venue             = excluded.venue,
                    date_range        = excluded.date_range,
                    last_availability = excluded.last_availability,
                    last_seen_at      = datetime('now'),
                    last_notified_at  = datetime('now')
                """,
                (
                    snapshot.kurs_id,
                    snapshot.title,
                    snapshot.course_number,
                    snapshot.district,
                    snapshot.venue,
                    snapshot.date_range,
                    snapshot.availability,
                ),
            )
    else:
        with conn:
            conn.execute(
                """
                INSERT INTO seen_courses (
                    kurs_id, title, course_number, district, venue, date_range,
                    last_availability
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kurs_id) DO UPDATE SET
                    title             = excluded.title,
                    course_number     = excluded.course_number,
                    district          = excluded.district,
                    venue             = excluded.venue,
                    date_range        = excluded.date_range,
                    last_availability = excluded.last_availability,
                    last_seen_at      = datetime('now')
                """,
                (
                    snapshot.kurs_id,
                    snapshot.title,
                    snapshot.course_number,
                    snapshot.district,
                    snapshot.venue,
                    snapshot.date_range,
                    snapshot.availability,
                ),
            )


# --- notification_log -------------------------------------------------------


VALID_NOTIFICATION_REASONS: frozenset[str] = frozenset({"new", "back_in_stock", "backfill"})

# Single source of truth for the four availability literals the parser emits
# and the diff classifier consumes. The site has historically used these exact
# strings (verified against the windows-1252 fixtures in tests/fixtures/); if a
# fifth literal ever appears, this is the one place to add it.
Availability = Literal[">2", "2", "1", "belegt"]
AVAILABILITY_LITERALS: frozenset[str] = frozenset({">2", "2", "1", "belegt"})
BOOKABLE_AVAILABILITY: frozenset[str] = frozenset({">2", "2", "1"})


def record_notification(
    conn: sqlite3.Connection, *, user_id: int, kurs_id: int, reason: str
) -> bool:
    """Returns True if the row was inserted, False if a matching row already existed.

    Callers should pass a value from :data:`VALID_NOTIFICATION_REASONS`. The
    storage layer does not enforce this — the PK includes ``reason``, so a
    typo would silently create a stuck row.
    """
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notification_log (user_id, kurs_id, reason) VALUES (?, ?, ?)",
            (user_id, kurs_id, reason),
        )
    return cur.rowcount > 0


def count_notifications_since(conn: sqlite3.Connection, *, user_id: int, since: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM notification_log WHERE user_id = ? AND sent_at >= ?",
        (user_id, since),
    ).fetchone()
    return row[0]
