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
from collections.abc import Callable, Iterable

import httpx

from vhsbot.db import CourseSnapshot
from vhsbot.parser import (
    FormState,
    has_next_page,
    parse_district_map,
    parse_form_state,
    parse_results_page,
)

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


RawHtmlCallback = Callable[[int, int, bytes], None]


async def crawl_district(
    *,
    client: httpx.AsyncClient,
    district_checkbox_index: int,
    sleep_seconds: float = 2.0,
    district_id: int | None = None,
    raw_html_callback: RawHtmlCallback | None = None,
) -> list[CourseSnapshot]:
    """Run the full search flow for one district. Returns all paginated rows.

    If ``raw_html_callback`` is provided, it is invoked once per result
    page with ``(district_id, page_idx, content_bytes)`` where
    ``page_idx`` is 0-indexed (page 1 of the results -> index 0). The
    callback receives only response bodies for *result pages*, not the
    intermediate form-setup POSTs. ``district_id`` is passed through
    unchanged from the caller; if omitted it defaults to the checkbox
    index (only the caller knows the real district id).
    """
    cb_district_id = district_id if district_id is not None else district_checkbox_index

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

    page_idx = 0
    if raw_html_callback is not None:
        raw_html_callback(cb_district_id, page_idx, resp.content)
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
        page_idx += 1
        if raw_html_callback is not None:
            raw_html_callback(cb_district_id, page_idx, resp.content)
        snapshots.extend(parse_results_page(resp.content))
    else:
        logger.warning("crawl_district hit max-pages guard (%s); stopping", _MAX_PAGES_GUARD)

    return snapshots


async def crawl(
    *,
    client: httpx.AsyncClient,
    district_ids: Iterable[int],
    sleep_seconds: float = 2.0,
    raw_html_callback: RawHtmlCallback | None = None,
) -> list[CourseSnapshot]:
    """Scrape multiple districts and return a dedup'd snapshot list.

    Performs one initial GET to build the ``district_id -> checkbox_index``
    map, validates that every requested id is known, then delegates to
    :func:`crawl_district` for each district in sorted order. Snapshots are
    dedup'd by ``kurs_id`` with first-occurrence wins (sorted district
    order makes the choice deterministic).

    An empty ``district_ids`` short-circuits to ``[]`` with no network
    activity.

    Note on sleeps: this function sleeps ``sleep_seconds`` between
    per-district crawls, and ``crawl_district`` itself sleeps
    ``sleep_seconds`` before its opening request. The effective gap
    between districts is therefore ``2 * sleep_seconds``. This doubling
    is intentional politeness rather than a bug — districts are scanned
    daily, not in tight loops.
    """
    requested = sorted(set(district_ids))
    if not requested:
        return []

    initial = await client.get(_SEARCH_URL)
    district_map = parse_district_map(initial.content)

    unknown = [d for d in requested if d not in district_map]
    if unknown:
        raise ValueError(f"unknown district id(s): {unknown}; known: {sorted(district_map)}")

    seen_kurs_ids: set[int] = set()
    out: list[CourseSnapshot] = []
    for i, district_id in enumerate(requested):
        if i > 0:
            await _sleep(sleep_seconds)
        checkbox_index = district_map[district_id]
        snapshots = await crawl_district(
            client=client,
            district_checkbox_index=checkbox_index,
            sleep_seconds=sleep_seconds,
            district_id=district_id,
            raw_html_callback=raw_html_callback,
        )
        for snap in snapshots:
            if snap.kurs_id in seen_kurs_ids:
                continue
            seen_kurs_ids.add(snap.kurs_id)
            out.append(snap)
    return out
