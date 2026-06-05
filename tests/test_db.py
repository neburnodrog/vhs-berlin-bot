"""Tests for SQLite storage layer (Phase 1)."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import set_last_availability

from vhsbot import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_init_schema_creates_all_tables(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "subscriptions",
        "user_settings",
        "seen_courses",
        "notification_log",
        "scan_log",
    } <= tables


def test_init_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    db.init_schema(conn)
    db.init_schema(conn)
    # No error, and tables still present
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone()[0]
    assert count == 1


# --- subscriptions ----------------------------------------------------------


def test_add_and_list_subscriptions(conn: sqlite3.Connection) -> None:
    db.add_subscription(conn, user_id=42, keyword="yoga")
    db.add_subscription(conn, user_id=42, keyword="töpfern")
    db.add_subscription(conn, user_id=99, keyword="other-user")

    assert db.list_subscriptions(conn, user_id=42) == ["töpfern", "yoga"]
    assert db.list_subscriptions(conn, user_id=99) == ["other-user"]
    assert db.list_subscriptions(conn, user_id=123) == []


def test_add_subscription_is_idempotent(conn: sqlite3.Connection) -> None:
    db.add_subscription(conn, user_id=42, keyword="yoga")
    db.add_subscription(conn, user_id=42, keyword="yoga")  # no error
    assert db.list_subscriptions(conn, user_id=42) == ["yoga"]


def test_remove_subscription_returns_true_when_present(conn: sqlite3.Connection) -> None:
    db.add_subscription(conn, user_id=42, keyword="yoga")
    assert db.remove_subscription(conn, user_id=42, keyword="yoga") is True
    assert db.list_subscriptions(conn, user_id=42) == []


def test_remove_subscription_returns_false_when_absent(conn: sqlite3.Connection) -> None:
    assert db.remove_subscription(conn, user_id=42, keyword="ghost") is False


def test_list_all_subscribed_user_ids(conn: sqlite3.Connection) -> None:
    db.add_subscription(conn, user_id=42, keyword="yoga")
    db.add_subscription(conn, user_id=42, keyword="töpfern")
    db.add_subscription(conn, user_id=99, keyword="other-user")
    assert db.all_subscribed_user_ids(conn) == [42, 99]


# --- user_settings ---------------------------------------------------------


def test_get_user_settings_returns_none_when_absent(conn: sqlite3.Connection) -> None:
    assert db.get_user_settings(conn, user_id=42) is None


def test_upsert_then_get_user_settings(conn: sqlite3.Connection) -> None:
    db.upsert_user_settings(conn, user_id=42, districts={31, 38, 39})

    settings = db.get_user_settings(conn, user_id=42)
    assert settings is not None
    assert settings.user_id == 42
    assert settings.districts == frozenset({31, 38, 39})
    assert settings.paused is False
    assert settings.include_waitlist is False


def test_upsert_user_settings_overwrites_districts(conn: sqlite3.Connection) -> None:
    db.upsert_user_settings(conn, user_id=42, districts={31})
    db.upsert_user_settings(conn, user_id=42, districts={38, 39})

    settings = db.get_user_settings(conn, user_id=42)
    assert settings is not None
    assert settings.districts == frozenset({38, 39})


def test_set_paused_flips_flag(conn: sqlite3.Connection) -> None:
    db.upsert_user_settings(conn, user_id=42, districts={31})

    db.set_paused(conn, user_id=42, paused=True)
    assert db.get_user_settings(conn, user_id=42).paused is True

    db.set_paused(conn, user_id=42, paused=False)
    assert db.get_user_settings(conn, user_id=42).paused is False


def test_all_active_user_ids_excludes_paused(conn: sqlite3.Connection) -> None:
    db.upsert_user_settings(conn, user_id=42, districts={31})
    db.upsert_user_settings(conn, user_id=99, districts={38})
    db.set_paused(conn, user_id=99, paused=True)

    assert db.all_active_user_ids(conn) == [42]


def test_union_of_districts_across_active_users(conn: sqlite3.Connection) -> None:
    db.upsert_user_settings(conn, user_id=42, districts={31, 38})
    db.upsert_user_settings(conn, user_id=99, districts={38, 39})
    db.upsert_user_settings(conn, user_id=7, districts={41})
    db.set_paused(conn, user_id=7, paused=True)  # paused — excluded

    assert db.union_active_districts(conn) == frozenset({31, 38, 39})


# --- seen_courses ----------------------------------------------------------


def _snap(**overrides) -> db.CourseSnapshot:
    base = dict(
        kurs_id=1000,
        title="Yoga für Anfänger",
        course_number="251A-12345",
        district="Mitte",
        venue="Linienstr. 162",
        date_range="2026-09-01 to 2026-12-15",
        availability=">2",
    )
    base.update(overrides)
    return db.CourseSnapshot(**base)


def test_get_seen_course_returns_none_when_absent(conn: sqlite3.Connection) -> None:
    assert db.get_seen_course(conn, kurs_id=1000) is None


def test_mark_seen_inserts_new_course(conn: sqlite3.Connection) -> None:
    db.mark_seen(conn, _snap(kurs_id=1000))

    seen = db.get_seen_course(conn, kurs_id=1000)
    assert seen is not None
    assert seen.kurs_id == 1000
    assert seen.title == "Yoga für Anfänger"
    assert seen.last_availability == ">2"
    assert seen.first_seen_at == seen.last_seen_at  # both set to "now" on insert
    assert seen.last_notified_at is None


def test_mark_seen_update_preserves_first_seen_at(conn: sqlite3.Connection) -> None:
    db.mark_seen(conn, _snap(kurs_id=1000, availability=">2"))
    original = db.get_seen_course(conn, kurs_id=1000)
    assert original is not None
    # Force a measurable timestamp gap (sqlite datetime('now') is second-precision).
    conn.execute(
        "UPDATE seen_courses SET first_seen_at = datetime('now', '-1 day'), "
        "last_seen_at = datetime('now', '-1 day') WHERE kurs_id = 1000"
    )
    conn.commit()
    earlier = db.get_seen_course(conn, kurs_id=1000)

    db.mark_seen(conn, _snap(kurs_id=1000, availability="belegt", title="Yoga renamed"))

    updated = db.get_seen_course(conn, kurs_id=1000)
    assert updated is not None
    assert updated.first_seen_at == earlier.first_seen_at  # preserved
    assert updated.last_seen_at > earlier.last_seen_at  # advanced
    assert updated.last_availability == "belegt"
    assert updated.title == "Yoga renamed"  # snapshot fields refresh


def test_get_seen_availability_bulk(conn: sqlite3.Connection) -> None:
    db.mark_seen(conn, _snap(kurs_id=1000, availability=">2"))
    db.mark_seen(conn, _snap(kurs_id=1001, availability="belegt"))

    result = db.get_seen_availability(conn, kurs_ids=[1000, 1001, 9999])
    assert result == {1000: ">2", 1001: "belegt"}


def test_mark_notified_sets_timestamp(conn: sqlite3.Connection) -> None:
    db.mark_seen(conn, _snap(kurs_id=1000))
    assert db.get_seen_course(conn, kurs_id=1000).last_notified_at is None

    db.mark_notified(conn, kurs_id=1000)

    assert db.get_seen_course(conn, kurs_id=1000).last_notified_at is not None


def test_upsert_seen_course_inserts_with_notified_false(conn: sqlite3.Connection) -> None:
    db.upsert_seen_course(conn, _snap(kurs_id=1000, availability=">2"), notified=False)
    seen = db.get_seen_course(conn, kurs_id=1000)
    assert seen is not None
    assert seen.last_availability == ">2"
    assert seen.last_notified_at is None  # not notified -> no timestamp


def test_upsert_seen_course_inserts_with_notified_true_sets_last_notified_at(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_seen_course(conn, _snap(kurs_id=1001, availability=">2"), notified=True)
    seen = db.get_seen_course(conn, kurs_id=1001)
    assert seen is not None
    assert seen.last_notified_at is not None


def test_upsert_seen_course_updates_last_seen_at_but_preserves_first_seen_at(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_seen_course(conn, _snap(kurs_id=1002, availability=">2"), notified=False)
    original = db.get_seen_course(conn, kurs_id=1002)
    assert original is not None

    # Backdate first_seen_at + last_seen_at so the update's bump is measurable.
    conn.execute(
        "UPDATE seen_courses SET first_seen_at = datetime('now', '-1 day'), "
        "last_seen_at = datetime('now', '-1 day') WHERE kurs_id = 1002"
    )
    conn.commit()
    earlier = db.get_seen_course(conn, kurs_id=1002)
    assert earlier is not None

    db.upsert_seen_course(conn, _snap(kurs_id=1002, availability="belegt"), notified=False)
    updated = db.get_seen_course(conn, kurs_id=1002)
    assert updated is not None
    assert updated.first_seen_at == earlier.first_seen_at
    assert updated.last_seen_at > earlier.last_seen_at
    assert updated.last_availability == "belegt"


# --- notification_log ------------------------------------------------------


def test_record_notification_first_time_returns_true(conn: sqlite3.Connection) -> None:
    assert db.record_notification(conn, user_id=42, kurs_id=1000, reason="new") is True


def test_record_notification_duplicate_returns_false(conn: sqlite3.Connection) -> None:
    db.record_notification(conn, user_id=42, kurs_id=1000, reason="new")
    assert db.record_notification(conn, user_id=42, kurs_id=1000, reason="new") is False


def test_record_notification_different_reason_is_new(conn: sqlite3.Connection) -> None:
    db.record_notification(conn, user_id=42, kurs_id=1000, reason="new")
    # Same user + course but different reason is allowed (back_in_stock later).
    assert db.record_notification(conn, user_id=42, kurs_id=1000, reason="back_in_stock") is True


def test_count_notifications_since(conn: sqlite3.Connection) -> None:
    # Insert two rows "today" and one row "yesterday" by overriding sent_at.
    db.record_notification(conn, user_id=42, kurs_id=1000, reason="new")
    db.record_notification(conn, user_id=42, kurs_id=1001, reason="new")
    db.record_notification(conn, user_id=42, kurs_id=1002, reason="new")
    conn.execute(
        "UPDATE notification_log SET sent_at = datetime('now', '-2 day') WHERE kurs_id = 1002"
    )
    conn.commit()

    since_today = "datetime('now', 'start of day')"
    cutoff = conn.execute(f"SELECT {since_today}").fetchone()[0]
    assert db.count_notifications_since(conn, user_id=42, since=cutoff) == 2
    assert db.count_notifications_since(conn, user_id=99, since=cutoff) == 0


# --- Phase 6 additions: seen_courses + notification window edge cases ------


def test_upsert_seen_course_with_notified_false_preserves_existing_last_notified_at(
    conn: sqlite3.Connection,
) -> None:
    """A no-notify upsert MUST keep the previous last_notified_at, not NULL it.

    Regression pin for the "still_full" branch of daily_scan: when a course
    that was previously notified-on appears again unchanged, we refresh
    ``last_seen_at`` but must NOT clobber ``last_notified_at`` back to NULL.
    """
    # First write: notified=True sets the timestamp.
    db.upsert_seen_course(conn, _snap(kurs_id=2000, availability=">2"), notified=True)
    first = db.get_seen_course(conn, kurs_id=2000)
    assert first is not None
    original_last_notified = first.last_notified_at
    assert original_last_notified is not None

    # Second write: notified=False on the same kurs_id — last_notified_at
    # must be preserved (not overwritten, not NULLed).
    db.upsert_seen_course(conn, _snap(kurs_id=2000, availability="belegt"), notified=False)
    updated = db.get_seen_course(conn, kurs_id=2000)
    assert updated is not None
    assert updated.last_notified_at == original_last_notified, (
        "notified=False upsert must NOT clobber a previously-set last_notified_at"
    )
    # And the other fields advanced as expected.
    assert updated.last_availability == "belegt"


def test_count_notifications_since_excludes_rows_outside_window(
    conn: sqlite3.Connection,
) -> None:
    """Boundary correctness for the cap-counter helper.

    Insert one row whose ``sent_at`` is JUST inside the trailing-24h window
    and another whose ``sent_at`` is JUST outside it. The counter must
    return exactly 1 when queried against a cutoff equal to "now - 24h".
    """
    # Row inside the window: 23h59m59s ago.
    db.record_notification(conn, user_id=42, kurs_id=3000, reason="new")
    conn.execute(
        "UPDATE notification_log SET "
        "sent_at = datetime('now', '-23 hours', '-59 minutes', '-59 seconds') "
        "WHERE kurs_id = 3000"
    )
    # Row outside the window: 24h00m01s ago.
    db.record_notification(conn, user_id=42, kurs_id=3001, reason="new")
    conn.execute(
        "UPDATE notification_log SET "
        "sent_at = datetime('now', '-24 hours', '-1 second') "
        "WHERE kurs_id = 3001"
    )
    conn.commit()

    cutoff = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', '-24 hours')").fetchone()[0]
    count = db.count_notifications_since(conn, user_id=42, since=cutoff)
    assert count == 1, (
        f"count_notifications_since must include the in-window row and exclude "
        f"the just-out-of-window row; got {count}"
    )


# --- scan_log (Phase 9) ----------------------------------------------------


def test_latest_scan_returns_none_when_empty(conn: sqlite3.Connection) -> None:
    assert db.latest_scan(conn) is None


def test_record_scan_inserts_row_with_default_scan_at(conn: sqlite3.Connection) -> None:
    db.record_scan(
        conn,
        districts_crawled=3,
        courses_seen=27,
        matches_sent=4,
        succeeded=True,
    )

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.districts_crawled == 3
    assert entry.courses_seen == 27
    assert entry.matches_sent == 4
    assert entry.succeeded is True
    assert entry.error_summary is None
    # scan_at auto-set by sqlite default.
    assert entry.scan_at  # non-empty string


def test_record_scan_failure_carries_error_summary(conn: sqlite3.Connection) -> None:
    db.record_scan(
        conn,
        districts_crawled=2,
        courses_seen=10,
        matches_sent=0,
        succeeded=False,
        error_summary="district 31: HTTPError 503",
    )

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.succeeded is False
    assert entry.error_summary == "district 31: HTTPError 503"


def test_record_scan_is_append_only_not_upsert(conn: sqlite3.Connection) -> None:
    """Two successive records must produce two rows, not overwrite one another."""
    db.record_scan(conn, districts_crawled=1, courses_seen=5, matches_sent=0, succeeded=True)
    db.record_scan(conn, districts_crawled=2, courses_seen=10, matches_sent=1, succeeded=True)

    row_count = conn.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
    assert row_count == 2


def test_latest_scan_returns_most_recent_row(conn: sqlite3.Connection) -> None:
    """When multiple rows exist, ``latest_scan`` returns the one with the highest id."""
    db.record_scan(conn, districts_crawled=1, courses_seen=5, matches_sent=0, succeeded=True)
    db.record_scan(conn, districts_crawled=2, courses_seen=10, matches_sent=1, succeeded=True)
    db.record_scan(
        conn,
        districts_crawled=3,
        courses_seen=15,
        matches_sent=2,
        succeeded=False,
        error_summary="boom",
    )

    entry = db.latest_scan(conn)
    assert entry is not None
    assert entry.districts_crawled == 3
    assert entry.succeeded is False
    assert entry.error_summary == "boom"


def test_set_last_availability_overwrites_only_that_column(conn: sqlite3.Connection) -> None:
    """``set_last_availability`` writes ``last_availability`` and nothing else.

    Pin for the test-seam helper used by e2e tests to flip a course to
    ``"belegt"`` so the next scan classifies it as ``back_in_stock``. It
    must NOT touch ``last_seen_at``, ``last_notified_at``, or any other
    column — those are owned by the production upsert path.

    The helper itself lives in :mod:`tests.conftest` (it is test-only —
    production code never overwrites ``last_availability`` directly).
    """
    db.upsert_seen_course(conn, _snap(kurs_id=4000, availability=">2"), notified=True)
    before = db.get_seen_course(conn, kurs_id=4000)
    assert before is not None
    assert before.last_availability == ">2"
    assert before.last_notified_at is not None

    set_last_availability(conn, kurs_id=4000, availability="belegt")

    after = db.get_seen_course(conn, kurs_id=4000)
    assert after is not None
    assert after.last_availability == "belegt"
    # Other columns untouched.
    assert after.last_notified_at == before.last_notified_at
    assert after.last_seen_at == before.last_seen_at
    assert after.first_seen_at == before.first_seen_at
    assert after.title == before.title
