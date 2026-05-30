"""HTTP orchestrator for the VHS Berlin course search flow.

Drives a single ``httpx.AsyncClient`` through the ASP.NET WebForms
sequence: GET form -> POST Erweitert tab -> POST search with district +
real submit button -> follow 302 to CourseList.aspx -> POST the
right-arrow image input until ``has_next_page`` is false. State is
re-parsed from every response, since the server mints fresh
``__VIEWSTATE``/``__EVENTVALIDATION`` values per turn.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from vhsbot.db import CourseSnapshot
from vhsbot.parser import FormState, has_next_page, parse_form_state, parse_results_page

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx"
_RESULTS_URL = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseList.aspx"
_NEXT_PAGE_INPUT = "ctl00$Content$ILDataGrid1$ctl01$ctl04"
_MAX_PAGES_GUARD = 50
_DISTRICT_CHECKBOX_NAME_RE = re.compile(
    r"^ctl00\$Content\$AreaListAdvanced1\$CheckBoxListDistricts\$(\d+)$"
)
_FORM_ENCODING = "windows-1252"


def _state_fields(state: FormState) -> dict[str, str]:
    fields = {
        "__VIEWSTATE": state.viewstate,
        "__VIEWSTATEGENERATOR": state.viewstate_generator,
    }
    if state.event_validation is not None:
        fields["__EVENTVALIDATION"] = state.event_validation
    return fields


async def _sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


async def crawl_district(
    *,
    client: httpx.AsyncClient,
    district_checkbox_index: int,
    sleep_seconds: float = 2.0,
) -> list[CourseSnapshot]:
    """Run the full search flow for one district. Returns all paginated rows."""
    resp = await client.get(_SEARCH_URL)
    state = parse_form_state(resp.content)

    await _sleep(sleep_seconds)
    resp = await client.post(
        _SEARCH_URL,
        data={
            **_state_fields(state),
            "ctl00$Content$lbtnTab2": "Erweitert",
        },
    )
    state = parse_form_state(resp.content)

    await _sleep(sleep_seconds)
    checkbox_field = (
        f"ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts${district_checkbox_index}"
    )
    resp = await client.post(
        _SEARCH_URL,
        data={
            **_state_fields(state),
            checkbox_field: "on",
            "ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm": "",
            "ctl00$Content$btnSearch": "Suchen",
        },
    )
    state = parse_form_state(resp.content)

    snapshots: list[CourseSnapshot] = list(parse_results_page(resp.content))

    for _ in range(_MAX_PAGES_GUARD):
        if not has_next_page(resp.content):
            break
        await _sleep(sleep_seconds)
        resp = await client.post(
            _RESULTS_URL,
            data={
                **_state_fields(state),
                f"{_NEXT_PAGE_INPUT}.x": "5",
                f"{_NEXT_PAGE_INPUT}.y": "5",
            },
        )
        state = parse_form_state(resp.content)
        snapshots.extend(parse_results_page(resp.content))
    else:
        logger.warning("crawl_district hit max-pages guard (%s); stopping", _MAX_PAGES_GUARD)

    return snapshots


def parse_district_map(html_bytes: bytes) -> dict[int, int]:
    """Map ``district_id -> checkbox_index`` by reading the GET form HTML.

    The advanced-search tab renders each district as
    ``<input type="checkbox" name="...$CheckBoxListDistricts$<N>" value="<district_id>">``
    where ``N`` is the checkbox index used in POST bodies and ``value`` is
    the canonical district id stored on each course row.
    """
    if not html_bytes:
        return {}
    soup = BeautifulSoup(html_bytes.decode(_FORM_ENCODING, errors="replace"), "lxml")
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
        checkbox_index = int(match.group(1))
        # First-write-wins so an accidental duplicate row does not silently
        # overwrite the canonical mapping.
        out.setdefault(district_id, checkbox_index)
    return out


async def crawl(
    *,
    client: httpx.AsyncClient,
    district_ids: set[int],
    sleep_seconds: float = 2.0,
) -> list[CourseSnapshot]:
    """Scrape multiple districts and return a dedup'd snapshot list.

    Performs one initial GET to build the ``district_id -> checkbox_index``
    map, validates that every requested id is known, then delegates to
    :func:`crawl_district` for each district in sorted order. Snapshots are
    dedup'd by ``kurs_id`` with first-occurrence wins (sorted district
    order makes the choice deterministic). Sleeps ``sleep_seconds`` between
    per-district crawls in addition to the per-request sleeps inside
    ``crawl_district``.
    """
    initial = await client.get(_SEARCH_URL)
    district_map = parse_district_map(initial.content)

    unknown = sorted(d for d in district_ids if d not in district_map)
    if unknown:
        raise ValueError(f"unknown district id(s): {unknown}; known: {sorted(district_map)}")

    seen_kurs_ids: set[int] = set()
    out: list[CourseSnapshot] = []
    for i, district_id in enumerate(sorted(district_ids)):
        if i > 0:
            await _sleep(sleep_seconds)
        checkbox_index = district_map[district_id]
        snapshots = await crawl_district(
            client=client,
            district_checkbox_index=checkbox_index,
            sleep_seconds=sleep_seconds,
        )
        for snap in snapshots:
            if snap.kurs_id in seen_kurs_ids:
                continue
            seen_kurs_ids.add(snap.kurs_id)
            out.append(snap)
    return out
