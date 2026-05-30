"""Tests for the HTTP orchestrator driving the VHS Berlin search flow.

The transport replays captured fixtures keyed by request shape. After
page 2 is served once, the transport returns page-2 with the right-arrow
input stripped, signalling "last page" without needing a third captured
fixture.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from vhsbot.parser import parse_results_page
from vhsbot.scraper import crawl, crawl_district

FIXTURES = Path(__file__).parent / "fixtures"
_NEXT_BTN_INPUT_RE = re.compile(
    rb'<input[^>]*name="ctl00\$Content\$ILDataGrid1\$ctl01\$ctl04"[^>]*>',
    re.IGNORECASE,
)
_CHECKBOX_IDX_RE = re.compile(rb"CheckBoxListDistricts%24(\d+)=on")
# Captures the title link's href + the inner title text so we can rewrite
# both atomically per row.
_TITLE_LINK_RE = re.compile(rb'(<a href="CourseDetail\.aspx\?id=)(\d+)(")([^>]*>)([^<]+)(</a>)')


class _FixtureTransport(httpx.AsyncBaseTransport):
    """Replays the captured 4-stage flow plus a synthetic "no more pages" terminator."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._next_page_count = 0
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        self._page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        self._page_2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
        self._page_2_stripped = _NEXT_BTN_INPUT_RE.sub(b"", self._page_2)
        assert self._page_2_stripped != self._page_2, "regex must match the next-arrow input"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"btnSearch=Suchen" in body:
            return _html_response(self._page_1)

        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            self._next_page_count += 1
            if self._next_page_count == 1:
                return _html_response(self._page_2)
            return _html_response(self._page_2_stripped)

        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


class _DedupTransport(httpx.AsyncBaseTransport):
    """Per-district overlapping result transport for the dedup contract test.

    Watches every search POST for the district checkbox index, then synthesizes
    a single-page response whose ten ``kurs_id`` values are shifted by a
    per-district offset. The result sets overlap deliberately so dedup is
    actually exercised (not just trivially passing).

    Per-district config:
      - district 31 (checkbox index 5) -> kurs_ids 1000..1009, title sentinel "DISTRICT-31"
      - district 32 (checkbox index 2) -> kurs_ids 1005..1014, title sentinel "DISTRICT-32"

    Overlap: ids 1005..1009 appear in both; union has 15 unique ids.
    The first-write-wins contract means snapshots for the overlapping ids
    must come from district 31 (the lower-numbered district, which sorts
    first in ``crawl``'s sorted iteration).
    """

    _DISTRICT_BY_INDEX: ClassVar[dict[int, tuple[str, bytes]]] = {
        5: ("31", b"DISTRICT-31"),
        2: ("32", b"DISTRICT-32"),
    }
    _OFFSET_BY_INDEX: ClassVar[dict[int, int]] = {
        5: 1000,  # district 31 -> 1000..1009
        2: 1005,  # district 32 -> 1005..1014
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        # Start from page-1 and strip the next-arrow so each district returns
        # exactly one page (the test is about cross-district dedup, not
        # pagination — pagination is covered separately).
        page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        self._page_1_no_next = _NEXT_BTN_INPUT_RE.sub(b"", page_1)
        assert self._page_1_no_next != page_1, "stripping next-arrow must mutate the page"
        self._last_checkbox_idx: int | None = None

    def _render_page(self, checkbox_idx: int) -> bytes:
        offset = self._OFFSET_BY_INDEX[checkbox_idx]
        _, title_sentinel = self._DISTRICT_BY_INDEX[checkbox_idx]
        # Rewrite the ten title-link rows in order: both the href id (so the
        # ten ids become offset..offset+9) and the visible title text (prefix
        # the per-district sentinel so first-occurrence-wins is observable).
        counter = {"i": 0}

        def replace_row(match: re.Match[bytes]) -> bytes:
            new_id = offset + counter["i"]
            counter["i"] += 1
            href_open, _old_id, href_close, attrs, title_text, link_close = match.groups()
            new_title = title_sentinel + b" | " + title_text
            return href_open + str(new_id).encode() + href_close + attrs + new_title + link_close

        rewritten = _TITLE_LINK_RE.sub(replace_row, self._page_1_no_next)
        assert counter["i"] == 10, f"expected 10 title-link rewrites, got {counter['i']}"
        return rewritten

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)

        if request.method == "POST" and b"btnSearch=Suchen" in body:
            match = _CHECKBOX_IDX_RE.search(body)
            if match is None:
                raise AssertionError(f"search POST missing district checkbox: {body[:200]!r}")
            checkbox_idx = int(match.group(1))
            self._last_checkbox_idx = checkbox_idx
            return _html_response(self._render_page(checkbox_idx))

        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


def _html_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "text/html; charset=iso-8859-15"},
    )


async def test_crawl_district_paginates_until_no_next_page() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        snapshots = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert len(snapshots) == 30  # 3 result pages, 10 rows each
    assert all(s.kurs_id > 0 for s in snapshots)

    methods = [m for (m, _, _) in transport.calls]
    assert methods == ["GET", "POST", "POST", "POST", "POST"]


async def test_search_post_uses_real_submit_button_and_district_checkbox() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        await crawl_district(client=client, district_checkbox_index=5, sleep_seconds=0)

    search_post_body = next(
        body
        for (method, _, body) in transport.calls
        if method == "POST" and b"btnSearch=Suchen" in body
    )

    assert b"btnSearch=Suchen" in search_post_body
    assert b"CheckBoxListDistricts%245=on" in search_post_body
    assert b"txtSearchTerm=" in search_post_body
    assert b"__VIEWSTATE=" in search_post_body
    assert b"__VIEWSTATEGENERATOR=" in search_post_body


async def test_next_page_post_uses_image_input_coordinates() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        await crawl_district(client=client, district_checkbox_index=5, sleep_seconds=0)

    next_page_bodies = [
        body
        for (method, _, body) in transport.calls
        if method == "POST" and b"ctl01%24ctl04.x=" in body
    ]
    assert len(next_page_bodies) == 2

    first_next = next_page_bodies[0]
    assert b"ctl01%24ctl04.x=" in first_next
    assert b"ctl01%24ctl04.y=" in first_next
    assert b"__EVENTVALIDATION=" in first_next  # CourseList.aspx responses include it


async def test_crawl_two_districts_dedups_by_kurs_id() -> None:
    """Cross-district dedup with disjoint-but-overlapping result sets.

    District 31 yields ids 1000..1009, district 32 yields 1005..1014.
    The five overlapping ids (1005..1009) must collapse to one snapshot
    each, and the kept snapshot must come from district 31 (sorts first,
    so first-write-wins keeps it).
    """
    transport = _DedupTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        snapshots = await crawl(
            client=client,
            district_ids={31, 32},
            sleep_seconds=0,
        )

    kurs_ids = [s.kurs_id for s in snapshots]

    # (a) Result equals the union of both districts' id ranges.
    assert set(kurs_ids) == set(range(1000, 1015))
    # 15 unique ids total: 10 from district 31 + 5 non-overlapping from 32.
    assert len(snapshots) == 15

    # (b) Every id appears exactly once.
    assert len(kurs_ids) == len(set(kurs_ids))

    # (c) For overlapping ids, the kept snapshot came from district 31
    # (verified via the per-district title sentinel). The non-overlapping
    # tail (1010..1014) must carry the district-32 sentinel.
    by_id = {s.kurs_id: s for s in snapshots}
    for overlap_id in range(1005, 1010):
        assert "DISTRICT-31" in by_id[overlap_id].title, (
            f"overlap id {overlap_id} should keep district 31's snapshot "
            f"(first-occurrence wins), got: {by_id[overlap_id].title!r}"
        )
    for tail_id in range(1010, 1015):
        assert "DISTRICT-32" in by_id[tail_id].title


async def test_crawl_unknown_district_raises_value_error() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(ValueError, match="99999"):
            await crawl(
                client=client,
                district_ids={99999},
                sleep_seconds=0,
            )


async def test_crawl_empty_district_ids_returns_empty_list() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl(
            client=client,
            district_ids=set(),
            sleep_seconds=0,
        )

    assert result == []
    # Short-circuit before issuing the initial GET — no network traffic at all.
    assert transport.calls == []


async def test_crawl_single_district_returns_all_snapshots() -> None:
    """A single-district crawl returns every unique row across paginated pages.

    The fixture transport serves page_1, page_2, page_2_stripped (page_2 with
    the next-arrow removed). Since page_2_stripped's rows duplicate page_2's,
    dedup collapses them — 20 unique ids total. Also implicitly checks that
    the inter-district sleep does not fire when only one district is in play
    (the loop's ``if i > 0`` guard).
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        snapshots = await crawl(
            client=client,
            district_ids={31},
            sleep_seconds=0,
        )

    # page_1 (10 ids) + page_2 (10 disjoint ids); page_2_stripped repeats
    # page_2's ids and gets dedup'd out.
    assert len(snapshots) == 20
    assert all(s.kurs_id > 0 for s in snapshots)

    # The full union of page-1 and page-2 ids must be present.
    page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
    page_2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
    expected_ids = {c.kurs_id for c in parse_results_page(page_1)} | {
        c.kurs_id for c in parse_results_page(page_2)
    }
    assert {s.kurs_id for s in snapshots} == expected_ids
