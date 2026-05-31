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
from dataclasses import dataclass

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
# Empirically Tempelhof-Schöneberg returns 81+ pages of empty-search results
# (810+ courses); 150 is generous headroom that survives every known Berlin
# Bezirk while still capping a runaway pagination loop. Callers must inspect
# the returned ``CrawlResult.truncated`` flag — hitting this guard means the
# tail of the result set was silently dropped, and surfacing that to the
# user / scheduler matters more than the guard value itself.
#
# Public (no leading underscore) because callers in ``jobs.py`` reference
# it in a log line and ``test_scraper.py`` pins the lower bound as a
# regression sanity test.
MAX_PAGES_GUARD = 150


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """Outcome of a district crawl.

    ``truncated`` is True iff the crawl stopped because it hit
    ``MAX_PAGES_GUARD`` rather than because the result set was exhausted.
    Callers can surface this to the user / log so silent under-counting
    doesn't happen — the original bug this guard exposed was a user
    running ``/watch goldschmiede`` against Tempelhof-Schöneberg (81+
    pages) and being told "0 Treffer gesendet" when the crawl had only
    seen the first 50 pages.

    ``snapshots`` is a ``tuple`` rather than a ``list`` so the
    ``frozen=True`` immutability guarantee is not a false signal —
    a frozen dataclass blocks ``result.snapshots = []`` but NOT
    ``result.snapshots.append(...)`` when the field is a list. A tuple
    closes that loophole.
    """

    snapshots: tuple[CourseSnapshot, ...]
    truncated: bool


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
    keyword: str = "",
) -> CrawlResult:
    """Run the full search flow for one district, returning a :class:`CrawlResult`.

    The returned ``CrawlResult.snapshots`` carries every paginated row the
    crawl observed; ``truncated`` is True iff the pagination loop hit
    :data:`MAX_PAGES_GUARD` without seeing a "no next page" signal (i.e.
    the tail of the result set was silently dropped). The WARNING log
    line still fires in that case; the flag is the structural signal
    callers can act on.

    If ``raw_html_callback`` is provided, it is invoked once per result
    page with ``(district_id, page_idx, content_bytes)`` where
    ``page_idx`` is 0-indexed (page 1 of the results -> index 0). The
    callback receives only response bodies for *result pages*, not the
    intermediate form-setup POSTs. ``district_id`` is passed through
    unchanged from the caller; if omitted it defaults to the checkbox
    index (only the caller knows the real district id).

    ``keyword`` is sent as ``txtSearchTerm`` in the initial search POST.
    VHS Berlin's server-side filter is more liberal than our own
    :func:`vhsbot.matching.matches` (it returns false positives like
    "Zimmerpflanzen — Sauerstoffspender" for keyword "goldschmiede"), so
    callers MUST still run the local matcher on the returned snapshots
    for correctness. The keyword exists purely as a pagination-budget
    shrinker: when it hits a rare term, the server returns one short
    page with no next-arrow, and we early-exit after a single fetch
    instead of paginating through 80+ unfiltered pages. When the keyword
    matches enough rows that the server paginates, we follow the
    pagination as before and let the local matcher do the heavy lifting.
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
            "ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm": keyword,
            "ctl00$Content$btnSearch": "Suchen",
        },
    )
    state = parse_form_state(resp.content)

    page_idx = 0
    if raw_html_callback is not None:
        raw_html_callback(cb_district_id, page_idx, resp.content)
    snapshots: list[CourseSnapshot] = list(parse_results_page(resp.content))

    # Rare-keyword early-exit. VHS Berlin's server filters page 1 by the
    # keyword we just sent; a rare term returns a short page with no
    # next-arrow. Skipping the pagination loop in that case is a pure win
    # (1 fetch vs 80+ on a deep district), with no correctness cost
    # because we already have everything the server would have returned.
    if keyword and not snapshots and not has_next_page(resp.content):
        return CrawlResult(snapshots=(), truncated=False)

    truncated = False
    for _ in range(MAX_PAGES_GUARD):
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
        # Loop exhausted ``range(MAX_PAGES_GUARD)`` without ``break``. The
        # naive for/else would set ``truncated=True`` here unconditionally,
        # but that produces a false positive when the result set is EXACTLY
        # ``MAX_PAGES_GUARD + 1`` pages long and the terminator landed on
        # the page returned by the final loop iteration: the loop ran out
        # of budget at the same moment the data ran out. Re-check
        # ``has_next_page`` on the final response — a next-page genuinely
        # exists only when the server still advertised the arrow.
        if has_next_page(resp.content):
            logger.warning("crawl_district hit max-pages guard (%s); stopping", MAX_PAGES_GUARD)
            truncated = True

    return CrawlResult(snapshots=tuple(snapshots), truncated=truncated)


async def crawl(
    *,
    client: httpx.AsyncClient,
    district_ids: Iterable[int],
    sleep_seconds: float = 2.0,
    raw_html_callback: RawHtmlCallback | None = None,
    keyword: str = "",
) -> CrawlResult:
    """Scrape multiple districts and return a dedup'd snapshot list.

    Performs one initial GET to build the ``district_id -> checkbox_index``
    map, validates that every requested id is known, then delegates to
    :func:`crawl_district` for each district in sorted order. Snapshots are
    dedup'd by ``kurs_id`` with first-occurrence wins (sorted district
    order makes the choice deterministic).

    An empty ``district_ids`` short-circuits to ``CrawlResult((), False)``
    with no network activity.

    ``keyword`` is threaded through to :func:`crawl_district` for the
    rare-term pagination-budget shrinker. ``CrawlResult.truncated`` here
    is True iff *any* district truncated during the sweep — a single
    truncated district means the union result set is incomplete and the
    caller should surface that.

    Note on sleeps: this function sleeps ``sleep_seconds`` between
    per-district crawls, and ``crawl_district`` itself sleeps
    ``sleep_seconds`` before its opening request. The effective gap
    between districts is therefore ``2 * sleep_seconds``. This doubling
    is intentional politeness rather than a bug — districts are scanned
    daily, not in tight loops.
    """
    requested = sorted(set(district_ids))
    if not requested:
        return CrawlResult(snapshots=(), truncated=False)

    initial = await client.get(_SEARCH_URL)
    district_map = parse_district_map(initial.content)

    unknown = [d for d in requested if d not in district_map]
    if unknown:
        raise ValueError(f"unknown district id(s): {unknown}; known: {sorted(district_map)}")

    seen_kurs_ids: set[int] = set()
    out: list[CourseSnapshot] = []
    any_truncated = False
    for i, district_id in enumerate(requested):
        if i > 0:
            await _sleep(sleep_seconds)
        checkbox_index = district_map[district_id]
        district_result = await crawl_district(
            client=client,
            district_checkbox_index=checkbox_index,
            sleep_seconds=sleep_seconds,
            district_id=district_id,
            raw_html_callback=raw_html_callback,
            keyword=keyword,
        )
        if district_result.truncated:
            any_truncated = True
        for snap in district_result.snapshots:
            if snap.kurs_id in seen_kurs_ids:
                continue
            seen_kurs_ids.add(snap.kurs_id)
            out.append(snap)
    return CrawlResult(snapshots=tuple(out), truncated=any_truncated)
