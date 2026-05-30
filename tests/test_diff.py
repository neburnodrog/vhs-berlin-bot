"""Tests for course-state classification (Phase 3 diff)."""

from __future__ import annotations

import pytest

from vhsbot import diff
from vhsbot.db import CourseSnapshot, SeenCourse


def _current(availability: str = ">2") -> CourseSnapshot:
    return CourseSnapshot(
        kurs_id=1000,
        title="Yoga für Anfänger",
        course_number="251A-12345",
        district="Mitte",
        venue=None,
        date_range="2026-09-01 to 2026-12-15",
        availability=availability,
    )


def _previous(last_availability: str) -> SeenCourse:
    return SeenCourse(
        kurs_id=1000,
        title="Yoga für Anfänger",
        course_number="251A-12345",
        district="Mitte",
        venue=None,
        date_range="2026-09-01 to 2026-12-15",
        last_availability=last_availability,
        first_seen_at="2026-05-01 08:00:00",
        last_seen_at="2026-05-29 08:00:00",
        last_notified_at=None,
    )


@pytest.mark.parametrize("availability", [">2", "2", "1", "belegt"])
def test_classify_new_when_no_previous(availability: str) -> None:
    assert diff.classify(_current(availability), None) == "new"


@pytest.mark.parametrize("now", [">2", "2", "1"])
def test_classify_back_in_stock_when_was_belegt_now_bookable(now: str) -> None:
    assert diff.classify(_current(now), _previous("belegt")) == "back_in_stock"


def test_classify_still_full_when_was_belegt_still_belegt() -> None:
    assert diff.classify(_current("belegt"), _previous("belegt")) == "still_full"


@pytest.mark.parametrize(
    ("prev", "curr"),
    [
        (">2", ">2"),
        (">2", "2"),
        (">2", "1"),
        ("2", ">2"),
        ("2", "2"),
        ("2", "1"),
        ("1", ">2"),
        ("1", "2"),
        ("1", "1"),
    ],
)
def test_classify_unchanged_when_both_bookable(prev: str, curr: str) -> None:
    assert diff.classify(_current(curr), _previous(prev)) == "unchanged"


@pytest.mark.parametrize("prev", [">2", "2", "1"])
def test_classify_unchanged_when_was_bookable_now_belegt(prev: str) -> None:
    # Locked policy: we do not notify on going-out-of-stock.
    assert diff.classify(_current("belegt"), _previous(prev)) == "unchanged"


def test_classify_raises_on_unknown_availability() -> None:
    with pytest.raises(ValueError, match="foobar"):
        diff.classify(_current("foobar"), None)


def test_classify_result_type_is_reexported() -> None:
    # Callers should be able to import the literal alias from the module.
    assert hasattr(diff, "ClassifyResult")
