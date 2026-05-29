"""Tests for SQLite storage layer (Phase 1)."""

from __future__ import annotations

import sqlite3

import pytest

from vhsbot import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_init_schema_creates_all_four_tables(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"subscriptions", "user_settings", "seen_courses", "notification_log"} <= tables


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
