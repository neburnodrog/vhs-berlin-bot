"""HTML parsing for vhsit.berlin.de.

Pure functions over raw response bytes. No I/O, no httpx, no logging —
everything in here is fixture-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag

from vhsbot.db import CourseSnapshot

_ENCODING = "windows-1252"
_KURS_ID_RE = re.compile(r"CourseDetail\.aspx\?id=(\d+)", re.IGNORECASE)
_NEXT_PAGE_NAME_RE = re.compile(r"\$ILDataGrid1\$ctl01\$ctl04$")
_WS_RE = re.compile(r"\s+")
_DISTRICT_CHECKBOX_NAME_RE = re.compile(
    r"^ctl00\$Content\$AreaListAdvanced1\$CheckBoxListDistricts\$(\d+)$"
)


@dataclass(frozen=True, slots=True)
class FormState:
    viewstate: str
    viewstate_generator: str
    event_validation: str | None


def _decode(html_bytes: bytes) -> BeautifulSoup:
    if not html_bytes:
        return BeautifulSoup("", "lxml")
    return BeautifulSoup(html_bytes.decode(_ENCODING, errors="replace"), "lxml")


def _hidden(soup: BeautifulSoup, name: str) -> str | None:
    el = soup.find("input", {"name": name, "type": "hidden"})
    if el is None:
        return None
    value = el.get("value")
    if value is None:
        return None
    return str(value)


def parse_form_state(html_bytes: bytes) -> FormState:
    soup = _decode(html_bytes)
    return FormState(
        viewstate=_hidden(soup, "__VIEWSTATE") or "",
        viewstate_generator=_hidden(soup, "__VIEWSTATEGENERATOR") or "",
        event_validation=_hidden(soup, "__EVENTVALIDATION"),
    )


def _text(td: Tag | None) -> str:
    if td is None:
        return ""
    return _WS_RE.sub(" ", td.get_text(" ", strip=True)).strip()


def _availability(raw: str) -> str:
    """Normalize the places cell to one of the plan-locked literals."""
    cleaned = raw.replace(" ", "").lower()
    if cleaned == "belegt":
        return "belegt"
    if cleaned == ">2":
        return ">2"
    if cleaned == "2":
        return "2"
    if cleaned == "1":
        return "1"
    return raw  # surface unexpected literals to caller for logging


def _parse_row(tr: Tag) -> CourseSnapshot | None:
    title_cell = tr.select_one("td.DataGridItemCourseTitle a")
    if title_cell is None:
        return None
    href = str(title_cell.get("href", ""))
    m = _KURS_ID_RE.search(href)
    if m is None:
        return None
    kurs_id = int(m.group(1))

    return CourseSnapshot(
        kurs_id=kurs_id,
        title=_text(title_cell),
        course_number=_text(tr.select_one("td.DataGridItemCourseNumber")),
        district=_text(tr.select_one("td.DataGridItemDistrict")) or None,
        venue=None,  # not in CourseList.aspx rows; detail-page parsing is out of scope for v1
        date_range=_text(tr.select_one("td.DataGridItemCourseBegin")) or None,
        availability=_availability(_text(tr.select_one("td.DataGridItemPlaces"))),
    )


def parse_results_page(html_bytes: bytes) -> list[CourseSnapshot]:
    soup = _decode(html_bytes)
    rows = soup.select("tr.DataGridItem, tr.DataGridAlternatingItem")
    out: list[CourseSnapshot] = []
    for tr in rows:
        snap = _parse_row(tr)
        if snap is not None:
            out.append(snap)
    return out


def has_next_page(html_bytes: bytes) -> bool:
    """True when the result page has an active next-page image button.

    The arrow-right input is at column index 4 in the pager; at the last
    page the form replaces it with an inactive variant. We detect the
    presence of an enabled ``<input type="image">`` whose name matches
    the right-arrow position.
    """
    soup = _decode(html_bytes)
    for inp in soup.find_all("input", {"type": "image"}):
        name = str(inp.get("name", ""))
        if _NEXT_PAGE_NAME_RE.search(name):
            return True
    return False


def parse_district_map(html_bytes: bytes) -> dict[int, int]:
    """Map ``district_id -> checkbox_index`` by reading the GET form HTML.

    The advanced-search tab renders each district as
    ``<input type="checkbox" name="...$CheckBoxListDistricts$<N>" value="<district_id>">``
    where ``N`` is the checkbox index used in POST bodies and ``value`` is
    the canonical district id stored on each course row.

    The "Alle Bezirke" wildcard checkbox (district id 0) is intentionally
    dropped: it represents "all districts" and would short-circuit any
    callers that defensively pass district_id=0.
    """
    soup = _decode(html_bytes)
    out: dict[int, int] = {}
    for inp in soup.find_all("input", {"type": "checkbox"}):
        name = str(inp.get("name", ""))
        match = _DISTRICT_CHECKBOX_NAME_RE.match(name)
        if match is None:
            continue
        raw_value = inp.get("value")
        if raw_value is None:
            continue
        try:
            district_id = int(str(raw_value))
        except ValueError:
            continue
        if district_id == 0:
            # "Alle Bezirke" sentinel; not a real district.
            continue
        checkbox_index = int(match.group(1))
        # First-write-wins so an accidental duplicate row does not silently
        # overwrite the canonical mapping.
        out.setdefault(district_id, checkbox_index)
    return out
