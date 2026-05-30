"""Tests for the HTTP orchestrator driving the VHS Berlin search flow.

The transport replays captured fixtures keyed by request shape. After
page 2 is served once, the transport returns page-2 with the right-arrow
input stripped, signalling "last page" without needing a third captured
fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from vhsbot.scraper import crawl, crawl_district, parse_district_map

FIXTURES = Path(__file__).parent / "fixtures"
_NEXT_BTN_INPUT_RE = re.compile(
    rb'<input[^>]*name="ctl00\$Content\$ILDataGrid1\$ctl01\$ctl04"[^>]*>',
    re.IGNORECASE,
)


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


def test_parse_district_map_extracts_known_districts() -> None:
    html_bytes = (FIXTURES / "form-initial.html").read_bytes()
    district_map = parse_district_map(html_bytes)

    # Anchor: Mitte (district id 31) is at checkbox index 5.
    assert district_map[31] == 5
    # Berlin has 12 admin districts plus VHS-internal cross-district rows.
    assert len(district_map) >= 12


async def test_crawl_two_districts_dedups_by_kurs_id() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        # 31=Mitte (index 5) and 32=Friedrichshain-Kreuzberg (index 2). The
        # transport replays the same captured pages for both districts, so
        # every snapshot from district 32 overlaps with district 31's.
        snapshots = await crawl(
            client=client,
            district_ids={31, 32},
            sleep_seconds=0,
        )

    # The dedup contract: every kurs_id appears at most once in the result.
    kurs_ids = [s.kurs_id for s in snapshots]
    assert len(kurs_ids) == len(set(kurs_ids))
    # Page 1 + page 2 fixtures have 20 distinct kurs_ids together; dedup
    # collapses the duplicated cross-district rows down to that set.
    assert len(snapshots) == 20


async def test_crawl_unknown_district_raises_value_error() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(ValueError, match="99999"):
            await crawl(
                client=client,
                district_ids={99999},
                sleep_seconds=0,
            )
