"""Tests for HTML parsing of vhsit.berlin.de responses.

All tests load fixtures captured in tests/fixtures/ as raw bytes and decode
inside the parser, mirroring what the live HTTP client will pass in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vhsbot import parser
from vhsbot.db import CourseSnapshot

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def form_initial() -> bytes:
    return (FIXTURES / "form-initial.html").read_bytes()


@pytest.fixture
def page_1() -> bytes:
    return (FIXTURES / "search-district-31-page-1.html").read_bytes()


@pytest.fixture
def page_2() -> bytes:
    return (FIXTURES / "search-district-31-page-2.html").read_bytes()


# --- parse_results_page ----------------------------------------------------


def test_parse_results_page_extracts_ten_courses(page_1: bytes) -> None:
    courses = parser.parse_results_page(page_1)
    assert len(courses) == 10
    assert all(isinstance(c, CourseSnapshot) for c in courses)


def test_first_course_matches_fixture(page_1: bytes) -> None:
    first = parser.parse_results_page(page_1)[0]

    assert first.kurs_id == 768359
    assert first.course_number == "Mi501-311-O"
    assert first.district == "Mitte"
    # Umlauts must survive the windows-1252 → str decode round-trip.
    assert "durchführbar" in first.title
    assert first.title.startswith("Onlinetraining Excel-Basiswissen")
    assert first.availability == ">2"
    # Venue is not present in the result table — only on detail pages.
    assert first.venue is None


def test_availability_literals_match_plan(page_1: bytes) -> None:
    courses = parser.parse_results_page(page_1)
    valid = {">2", "2", "1", "belegt"}
    for course in courses:
        assert course.availability in valid, (
            f"unexpected availability {course.availability!r} for {course.kurs_id}"
        )


def test_kurs_ids_are_unique_within_a_page(page_1: bytes) -> None:
    ids = [c.kurs_id for c in parser.parse_results_page(page_1)]
    assert len(ids) == len(set(ids))


def test_page_2_also_parses_disjoint_from_page_1(page_1: bytes, page_2: bytes) -> None:
    courses_1 = parser.parse_results_page(page_1)
    courses_2 = parser.parse_results_page(page_2)
    assert len(courses_2) == 10
    # No kurs_id overlap — sanity check that pagination really advanced.
    assert {c.kurs_id for c in courses_1}.isdisjoint({c.kurs_id for c in courses_2})


def test_empty_input_returns_empty_list() -> None:
    assert parser.parse_results_page(b"") == []
    assert parser.parse_results_page(b"<html><body>nothing here</body></html>") == []


# --- parse_form_state ------------------------------------------------------


def test_form_state_on_initial_get(form_initial: bytes) -> None:
    state = parser.parse_form_state(form_initial)
    assert state.viewstate  # non-empty
    assert state.viewstate_generator == "2B79C7F0"
    # The initial GET does not include __EVENTVALIDATION.
    assert state.event_validation is None


def test_form_state_on_results_page(page_1: bytes) -> None:
    state = parser.parse_form_state(page_1)
    assert state.viewstate
    assert state.viewstate_generator == "03F8BC54"
    assert state.event_validation  # present on CourseList.aspx


# --- pagination ------------------------------------------------------------


def test_has_next_page_on_page_1(page_1: bytes) -> None:
    # 312 results / 10 per page = 32 pages; page 1 must have a next button.
    assert parser.has_next_page(page_1) is True


def test_has_next_page_on_page_2(page_2: bytes) -> None:
    # Page 2 of 32 — still more to go.
    assert parser.has_next_page(page_2) is True


def test_has_next_page_on_initial_form(form_initial: bytes) -> None:
    # The search form before any search has no results table at all.
    assert parser.has_next_page(form_initial) is False
