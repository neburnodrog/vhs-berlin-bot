"""Tests for HTML parsing of vhsit.berlin.de responses.

All tests load fixtures captured in tests/fixtures/ as raw bytes and decode
inside the parser, mirroring what the live HTTP client will pass in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vhsbot import parser
from vhsbot.db import AVAILABILITY_LITERALS, CourseSnapshot

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
    for course in courses:
        assert course.availability in AVAILABILITY_LITERALS, (
            f"unexpected availability {course.availability!r} for {course.kurs_id}"
        )


def test_availability_normalizer_returns_only_canonical_literals_for_known_inputs() -> None:
    # Cross-module invariant: every known input form for the places cell
    # (including whitespace + case-noise variants the site has emitted) must
    # normalize to a value drawn from the single source of truth in db.py.
    # If a future site change adds a fifth literal, this test catches it
    # together with the diff classifier's ValueError guard.
    known_inputs = [
        ">2",
        " > 2 ",
        "2",
        "1",
        "belegt",
        "BELEGT",
        " Belegt ",
    ]
    for raw in known_inputs:
        assert parser._availability(raw) in AVAILABILITY_LITERALS, (
            f"normalizer leaked non-canonical value for input {raw!r}"
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


def test_has_next_page_false_when_pager_lacks_right_arrow() -> None:
    # Synthetic: a results-shaped page where the pager has prev/label/last
    # but no right-arrow ($ctl04). Simulates the last page of a paginated set.
    html = b"""<html><body>
        <table><tr class="DataGridItem"><td>row</td></tr></table>
        <input type="image" name="ctl00$Content$ILDataGrid1$ctl01$ctl01" src="leftend.svg"/>
        <input type="image" name="ctl00$Content$ILDataGrid1$ctl01$ctl02" src="left.svg"/>
        <input type="image" name="ctl00$Content$ILDataGrid1$ctl01$ctl05" src="rightend.svg"/>
    </body></html>"""
    assert parser.has_next_page(html) is False


# --- parse_district_map ----------------------------------------------------


def test_parse_district_map_extracts_known_districts(form_initial: bytes) -> None:
    district_map = parser.parse_district_map(form_initial)

    # Anchor: Mitte (district id 31) is at checkbox index 5.
    assert district_map[31] == 5
    # Berlin has 12 admin districts plus VHS-internal cross-district rows.
    assert len(district_map) >= 12
    # The "Alle Bezirke" wildcard (district id 0) must not leak through.
    assert 0 not in district_map


def test_parse_district_map_returns_empty_dict_on_empty_bytes() -> None:
    assert parser.parse_district_map(b"") == {}


def test_parse_district_map_skips_non_int_value() -> None:
    # value="abc" cannot be coerced to int — the parser must skip silently
    # rather than crash the whole map.
    html = (
        b"<html><body>"
        b'<input type="checkbox" '
        b'name="ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$0" '
        b'value="abc">'
        b"</body></html>"
    )
    assert parser.parse_district_map(html) == {}


def test_parse_district_map_first_write_wins_on_duplicate_district() -> None:
    # Two checkboxes claim district 31 at indexes 5 and 7. First-write-wins
    # means the lower index sticks; the duplicate must not overwrite.
    html = (
        b"<html><body>"
        b'<input type="checkbox" '
        b'name="ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$5" '
        b'value="31">'
        b'<input type="checkbox" '
        b'name="ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$7" '
        b'value="31">'
        b"</body></html>"
    )
    result = parser.parse_district_map(html)
    assert result == {31: 5}


def test_parse_district_map_excludes_alle_bezirke_sentinel(form_initial: bytes) -> None:
    # The form's first district checkbox is "Alle Bezirke" with value="0";
    # callers must not be able to pass district_id=0 through validation.
    assert 0 not in parser.parse_district_map(form_initial)


# --- parse_district_names --------------------------------------------------


def test_parse_district_names_extracts_human_labels(form_initial: bytes) -> None:
    # Anchors the full 12-Bezirk map plus the two VHS-internal cross-district
    # rows the live site exposes. The "alle" sentinel (district_id=0) must
    # NOT leak through — same exclusion rule as ``parse_district_map``.
    # German diacritics must survive the windows-1252 → str decode round-trip;
    # they go straight into a Telegram button label as UTF-8.
    names = parser.parse_district_names(form_initial)
    assert 0 not in names
    assert names[31] == "Mitte"
    assert names[32] == "Friedrichshain-Kreuzberg"
    assert names[33] == "Pankow"
    assert names[34] == "Charlottenburg-Wilmersdorf"
    assert names[35] == "Spandau"
    assert names[36] == "Steglitz-Zehlendorf"
    assert names[37] == "Tempelhof-Schöneberg"
    assert names[38] == "Neukölln"
    assert names[39] == "Treptow-Köpenick"
    assert names[40] == "Marzahn-Hellersdorf"
    assert names[41] == "Lichtenberg"
    assert names[42] == "Reinickendorf"
    assert names[81] == "Servicezentrum und zentrale Prüfungen"
    assert names[98] == "zentrale Kursleiterfortbildung"


def test_parse_district_names_returns_empty_dict_on_empty_bytes() -> None:
    assert parser.parse_district_names(b"") == {}


def test_parse_district_names_falls_back_to_str_district_id_when_label_missing() -> None:
    # Defensive: if a checkbox has no matching ``<label for="...">`` we must
    # NOT blow up onboarding for the other districts — fall back to the
    # string form of the district id for that one entry.
    html = (
        b"<html><body>"
        b'<input id="ctl00_X_CheckBoxListDistricts_5" type="checkbox" '
        b'name="ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$5" '
        b'value="31">'
        b'<label for="ctl00_X_CheckBoxListDistricts_5">Mitte</label>'
        b'<input id="ctl00_X_CheckBoxListDistricts_6" type="checkbox" '
        b'name="ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$6" '
        b'value="39">'
        # No <label for="..._6"> on purpose.
        b"</body></html>"
    )
    names = parser.parse_district_names(html)
    assert names[31] == "Mitte"
    assert names[39] == "39"


# --- Phase 9 review-fix: defensive availability parsing -------------------


def _row_html(kurs_id: int, places_inner: str) -> bytes:
    """Synthesize a single CourseList.aspx-shaped row with a configurable
    "Places" cell. Used to exercise the boundary between the parser and the
    diff classifier without depending on the captured fixtures.
    """
    return (
        b"<html><body><table>"
        b'<tr class="DataGridItem">'
        b'<td class="DataGridItemCourseTitle">'
        b'<a href="CourseDetail.aspx?id=' + str(kurs_id).encode() + b'">Test Course</a>'
        b"</td>"
        b'<td class="DataGridItemCourseNumber">XYZ-001</td>'
        b'<td class="DataGridItemDistrict">Mitte</td>'
        b'<td class="DataGridItemCourseBegin">01.06.2026</td>'
        b'<td class="DataGridItemPlaces">' + places_inner.encode() + b"</td>"
        b"</tr></table></body></html>"
    )


def test_parse_results_drops_row_with_empty_availability() -> None:
    """A row whose Places cell is empty must be silently dropped at the
    parser boundary.

    Phase 9 incident: a single empty-availability row triggered a
    ValueError in ``diff.classify`` and aborted the entire scan after
    1290 courses had already been processed. The fix is defensive
    parsing at the boundary: rows we cannot normalize to a canonical
    availability literal are dropped before they reach the classifier.
    """
    html = _row_html(kurs_id=99001, places_inner="")
    snapshots = parser.parse_results_page(html)
    assert snapshots == [], "an empty-availability row must be dropped at the parser boundary"


def test_parse_results_drops_row_with_unknown_availability_literal() -> None:
    """A row whose Places cell does not normalize to one of the four
    canonical literals (``>2 | 2 | 1 | belegt``) must also be dropped.
    """
    html = _row_html(kurs_id=99002, places_inner="verfügbar")  # fictional literal
    snapshots = parser.parse_results_page(html)
    assert snapshots == []


def test_parse_results_logs_warning_when_row_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping a bad row must surface in the operator's log so upstream
    data corruption is visible. The log must include the ``kurs_id`` so
    investigations can pinpoint the offending course.
    """
    import logging as _logging

    html = _row_html(kurs_id=99003, places_inner="")
    with caplog.at_level(_logging.WARNING, logger="vhsbot.parser"):
        parser.parse_results_page(html)
    assert any("99003" in rec.message for rec in caplog.records), (
        f"WARNING must mention dropped kurs_id; saw: {[r.message for r in caplog.records]!r}"
    )


def test_parse_results_keeps_row_with_canonical_availability() -> None:
    """Sanity: the defensive filter must NOT drop legitimate rows. The
    four canonical literals (and their case/whitespace variants the
    normalizer accepts) all pass through.
    """
    for literal in (">2", "2", "1", "belegt", "BELEGT", " > 2 "):
        html = _row_html(kurs_id=99100, places_inner=literal)
        snapshots = parser.parse_results_page(html)
        assert len(snapshots) == 1, (
            f"canonical input {literal!r} should NOT be dropped by defensive parsing"
        )
        assert snapshots[0].availability in AVAILABILITY_LITERALS
