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


def test_matches_preserves_input_order_not_haystack_order() -> None:
    # The keywords are passed in reverse haystack order: "anfänger" appears
    # AFTER "yoga" in the title, but it comes FIRST in the input. A buggy
    # implementation that walked the haystack would return ["yoga","anfänger"];
    # the contract is input order, so we must get ["anfänger","yoga"].
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["anfänger", "yoga"]) == ["anfänger", "yoga"]


def test_matches_keyword_with_space_can_bridge_title_and_course_number() -> None:
    # Pinned for regression detection; behavior may be revisited in Phase 4
    # when subscription UX lands. The haystack is fold(title) + " " +
    # fold(course_number), so a keyword that itself contains the boundary
    # whitespace can match across the join. This is a known corner; the test
    # documents the current semantics rather than endorses them.
    course = _course(title="3", course_number="K-FR-01")
    assert matching.matches(course, ["3 k"]) == ["3 k"]


# --- fold + matches edge cases --------------------------------------------


def test_fold_empty_string_returns_empty() -> None:
    assert matching.fold("") == ""


def test_fold_whitespace_only_returns_empty() -> None:
    assert matching.fold("   \n\t  ") == ""


def test_matches_empty_keyword_iterable_returns_empty_list() -> None:
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, []) == []


def test_matches_single_char_keyword_hits_course_number() -> None:
    # The folded haystack is lowercase, so "k" matches K-FR-01's "k".
    course = _course(title="Irrelevant Title", course_number="K-FR-01")
    assert matching.matches(course, ["k"]) == ["k"]


def test_matches_dedups_case_only_duplicates() -> None:
    # All three fold to the same form; the FIRST input's casing wins.
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["Yoga", "yoga", "YOGA"]) == ["Yoga"]


def test_matches_dedups_diacritic_only_duplicates() -> None:
    # "Anfänger" and "anfanger" fold to the same form; only the first wins.
    course = _course(title="Yoga für Anfänger", course_number="K-X-01")
    assert matching.matches(course, ["Anfänger", "anfanger"]) == ["Anfänger"]
