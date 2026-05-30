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

import httpx

from vhsbot.db import CourseSnapshot
from vhsbot.parser import FormState, has_next_page, parse_form_state, parse_results_page

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx"
_RESULTS_URL = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseList.aspx"
_NEXT_PAGE_INPUT = "ctl00$Content$ILDataGrid1$ctl01$ctl04"
_MAX_PAGES_GUARD = 50


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
