"""Tests for Unicode-folded substring matching of keywords against courses."""

from __future__ import annotations

import pytest

from vhsbot import matching
from vhsbot.db import CourseSnapshot


def _course(**overrides: object) -> CourseSnapshot:
    base: dict[str, object] = {
        "kurs_id": 1000,
        "title": "Yoga für Anfänger",
        "course_number": "251A-12345",
        "district": "Mitte",
        "venue": None,
        "date_range": "2026-09-01 to 2026-12-15",
        "availability": ">2",
    }
    base.update(overrides)
    return CourseSnapshot(**base)  # type: ignore[arg-type]


# --- fold ------------------------------------------------------------------


def test_fold_collapses_umlauts_and_case() -> None:
    assert matching.fold("Französisch") == "franzosisch"
    assert matching.fold("französisch") == "franzosisch"
    assert matching.fold("FRANZÖSISCH") == "franzosisch"


def test_fold_handles_ess_zett() -> None:
    assert matching.fold("Straße") == matching.fold("strasse") == "strasse"


@pytest.mark.parametrize(
    "raw",
    [
        "Französisch",
        "Yoga für Anfänger",
        "  multi   space\nlines  ",
        "Straße",
        "",
        "K-FR-01",
    ],
)
def test_fold_is_idempotent(raw: str) -> None:
    once = matching.fold(raw)
    assert matching.fold(once) == once


def test_fold_collapses_whitespace_runs() -> None:
    assert matching.fold("  multi   space\nlines  ") == "multi space lines"


# --- matches ---------------------------------------------------------------


def test_matches_returns_hit_keyword_for_title_substring() -> None:
    course = _course(title="Einführung in Französisch", course_number="K-X-01")
    assert matching.matches(course, ["französisch"]) == ["französisch"]


def test_matches_is_case_insensitive() -> None:
    course = _course(title="Einführung in Französisch", course_number="K-X-01")
    # Original casing is preserved in the returned list.
    assert matching.matches(course, ["FRANZÖSISCH"]) == ["FRANZÖSISCH"]


def test_matches_hits_course_number_field() -> None:
    course = _course(title="Irrelevant Title", course_number="K-FR-01")
    assert matching.matches(course, ["K-FR"]) == ["K-FR"]


def test_matches_or_across_multiple_keywords() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["yoga", "pilates"]) == ["yoga"]


def test_matches_returns_all_matching_keywords() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["yoga", "anfänger"]) == ["yoga", "anfänger"]


def test_matches_dedups_repeated_keywords() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["yoga", "yoga", "yoga"]) == ["yoga"]


def test_matches_skips_empty_keywords() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["", "  ", "yoga"]) == ["yoga"]


def test_matches_returns_empty_when_no_keyword_hits() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["italienisch"]) == []
