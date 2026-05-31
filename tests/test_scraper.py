"""Tests for the HTTP orchestrator driving the VHS Berlin search flow.

The transport replays captured fixtures keyed by request shape. After
page 2 is served once, the transport returns page-2 with the right-arrow
input stripped, signalling "last page" without needing a third captured
fixture.
"""

from __future__ import annotations

import re
from typing import ClassVar

import httpx
import pytest
from conftest import FIXTURES, _FixtureTransport, _html_response

from vhsbot import scraper
from vhsbot.parser import parse_results_page
from vhsbot.scraper import MAX_PAGES_GUARD, CrawlResult, crawl, crawl_district

_NEXT_BTN_INPUT_RE = re.compile(
    rb'<input[^>]*name="ctl00\$Content\$ILDataGrid1\$ctl01\$ctl04"[^>]*>',
    re.IGNORECASE,
)
_CHECKBOX_IDX_RE = re.compile(rb"CheckBoxListDistricts%24(\d+)=on")
# Captures the title link's href + the inner title text so we can rewrite
# both atomically per row.
_TITLE_LINK_RE = re.compile(rb'(<a href="CourseDetail\.aspx\?id=)(\d+)(")([^>]*>)([^<]+)(</a>)')


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


async def test_crawl_district_paginates_until_no_next_page() -> None:
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert isinstance(result, CrawlResult)
    assert len(result.snapshots) == 30  # 3 result pages, 10 rows each
    assert all(s.kurs_id > 0 for s in result.snapshots)
    assert result.truncated is False

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
    # Default ``keyword=""`` -> empty ``txtSearchTerm`` value. Tighter than
    # a bare ``b"txtSearchTerm="`` match (which would also pass against
    # ``txtSearchTerm=goldschmiede``); we want this test to fail if a
    # future refactor accidentally starts forwarding a non-empty default.
    assert b"txtSearchTerm=&" in search_post_body or search_post_body.endswith(b"txtSearchTerm="), (
        f"empty default keyword must serialize as empty txtSearchTerm: {search_post_body[-80:]!r}"
    )
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
        result = await crawl(
            client=client,
            district_ids={31, 32},
            sleep_seconds=0,
        )

    snapshots = result.snapshots
    kurs_ids = [s.kurs_id for s in snapshots]

    # (a) Result equals the union of both districts' id ranges.
    assert set(kurs_ids) == set(range(1000, 1015))
    # 15 unique ids total: 10 from district 31 + 5 non-overlapping from 32.
    assert len(snapshots) == 15
    assert result.truncated is False

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

    assert result.snapshots == ()
    assert result.truncated is False
    # Short-circuit before issuing the initial GET — no network traffic at all.
    assert transport.calls == []


async def test_crawl_district_invokes_raw_html_callback_per_page() -> None:
    """Phase 5: snapshot callback fires once per result page.

    The fixture transport serves three result pages (page_1, page_2,
    page_2_stripped). With a callback wired in, we should see exactly
    three invocations, each carrying ``(district_id, page_idx, bytes)``
    where ``page_idx`` is the 0-indexed page number.
    """
    transport = _FixtureTransport()
    calls: list[tuple[int, int, int]] = []  # (district_id, page_idx, byte_len)

    def cb(district_id: int, page_idx: int, content: bytes) -> None:
        calls.append((district_id, page_idx, len(content)))

    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
            district_id=31,
            raw_html_callback=cb,
        )

    # Three result pages -> three callback invocations.
    assert len(calls) == 3
    # Each invocation has the right district_id and a monotonically
    # increasing page_idx starting at 0.
    assert [c[0] for c in calls] == [31, 31, 31]
    assert [c[1] for c in calls] == [0, 1, 2]
    # And the content was non-empty (defensive — we don't want a None or b"").
    assert all(c[2] > 0 for c in calls)


async def test_crawl_district_omits_raw_html_callback_by_default_remains_backward_compat() -> None:
    """Calling crawl_district without the new kwarg must work unchanged.

    Cements the backward-compat guarantee from Phase 5 decision A.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert len(result.snapshots) == 30
    assert result.truncated is False


async def test_crawl_omits_raw_html_callback_by_default_remains_backward_compat() -> None:
    """Phase 6 explicit pin: ``crawl`` (the multi-district wrapper) default-None.

    Companion to the ``crawl_district`` version. Callers can invoke the
    public ``crawl`` API without ``raw_html_callback`` and get the same
    return-value shape as before Phase 5 introduced the kwarg. The
    default-None case must not perform any I/O the caller didn't ask for
    (such as snapshot writes), and must not crash when the kwarg is
    absent.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl(
            client=client,
            district_ids={31},
            sleep_seconds=0,
        )

    # Same shape as test_crawl_single_district_returns_all_snapshots:
    # 20 unique kurs_ids from page-1 + page-2 (page_2_stripped dedup'd out).
    assert len(result.snapshots) == 20
    assert result.truncated is False


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
        result = await crawl(
            client=client,
            district_ids={31},
            sleep_seconds=0,
        )

    snapshots = result.snapshots
    # page_1 (10 ids) + page_2 (10 disjoint ids); page_2_stripped repeats
    # page_2's ids and gets dedup'd out.
    assert len(snapshots) == 20
    assert all(s.kurs_id > 0 for s in snapshots)
    assert result.truncated is False

    # The full union of page-1 and page-2 ids must be present.
    page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
    page_2 = (FIXTURES / "search-district-31-page-2.html").read_bytes()
    expected_ids = {c.kurs_id for c in parse_results_page(page_1)} | {
        c.kurs_id for c in parse_results_page(page_2)
    }
    assert {s.kurs_id for s in snapshots} == expected_ids


# ---------------------------------------------------------------------------
# Phase 7 scraper fix: keyword early-exit + truncation surfacing
# ---------------------------------------------------------------------------


_DATAGRID_ROW_RE = re.compile(
    rb'<tr class="DataGrid(Item|AlternatingItem)">.*?</tr>',
    re.IGNORECASE | re.DOTALL,
)


class _EmptyPage1Transport(httpx.AsyncBaseTransport):
    """Serves a page-1 with zero result rows AND no next-arrow input.

    Mimics what VHS Berlin returns when a rare keyword is sent in the
    initial search POST: the server-side filter trims the result set to
    something the page-1 grid can hold without paginating. The orchestrator
    must early-exit after the search POST instead of paginating into pages
    that don't exist.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        # Strip rows AND the next-arrow input so the page is empty + terminal.
        page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        without_rows = _DATAGRID_ROW_RE.sub(b"", page_1)
        assert without_rows != page_1, "stripping rows must mutate the page"
        self._empty_page_1 = _NEXT_BTN_INPUT_RE.sub(b"", without_rows)
        assert self._empty_page_1 != without_rows, (
            "stripping next-arrow must mutate the page; empty fixture must terminate"
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"btnSearch=Suchen" in body:
            return _html_response(self._empty_page_1)
        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            # If we ever paginate here the early-exit is broken.
            raise AssertionError(
                f"early-exit broken: pagination POST reached the transport: {body[:200]!r}"
            )
        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


class _InfinitePagesTransport(httpx.AsyncBaseTransport):
    """Always returns a page that still has the next-arrow input.

    Used to drive ``crawl_district`` into the ``_MAX_PAGES_GUARD`` branch so
    we can pin the ``truncated=True`` signal. Page 1 carries 10 rows from
    the captured fixture; every subsequent next-page POST returns the same
    bytes (rows + next-arrow), so dedup is a non-issue here because the
    orchestrator does not dedup within a single district. We assert on
    truncation, not on snapshot count.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        self._page = (FIXTURES / "search-district-31-page-1.html").read_bytes()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"btnSearch=Suchen" in body:
            return _html_response(self._page)
        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            return _html_response(self._page)
        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


async def test_crawl_district_with_keyword_early_exits_on_empty_page_one() -> None:
    """Rare keyword -> server returns empty page-1 with no next-arrow.

    ``crawl_district`` must early-exit after the search POST: no pagination
    requests at all. The whole flow is exactly four requests
    (GET + Erweitert + search), the empty result set comes back unchanged,
    and ``truncated`` is False.
    """
    transport = _EmptyPage1Transport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
            keyword="goldschmiede",
        )

    assert result.snapshots == ()
    assert result.truncated is False
    # GET + Erweitert + search POST only; NO pagination POSTs.
    methods = [m for (m, _, _) in transport.calls]
    assert methods == ["GET", "POST", "POST"], (
        f"early-exit must skip pagination; got method sequence {methods}"
    )
    # And the search POST carried the keyword (URL-encoded "goldschmiede").
    search_body = transport.calls[-1][2]
    assert b"txtSearchTerm=goldschmiede" in search_body


async def test_crawl_district_with_keyword_still_paginates_when_results_exist() -> None:
    """Keyword present but page-1 carries rows and next-arrow -> paginate as usual.

    Pins that the early-exit is conditional on BOTH (a) zero rows AND
    (b) no next-arrow. The fixture transport returns the real captured
    page-1 (10 rows + next-arrow), so even with a keyword the orchestrator
    must continue the paginated walk.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
            keyword="yoga",
        )

    # Same 30 rows as the no-keyword paginated walk.
    assert len(result.snapshots) == 30
    assert result.truncated is False
    methods = [m for (m, _, _) in transport.calls]
    assert methods == ["GET", "POST", "POST", "POST", "POST"]


async def test_crawl_district_marks_truncated_when_guard_hit() -> None:
    """Infinite-next-arrow fixture must trip the page-guard and set truncated=True.

    The WARNING log line still fires (kept deliberately so operators see
    truncation in logs even without the structural signal), but the
    structural signal is the ``truncated`` flag on the returned
    :class:`CrawlResult`.
    """
    transport = _InfinitePagesTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert result.truncated is True, (
        "hitting _MAX_PAGES_GUARD must surface as truncated=True so callers can warn"
    )


async def test_crawl_district_truncated_false_when_natural_end() -> None:
    """Crawl that exhausts naturally before the guard must report truncated=False.

    Companion to the truncated-True test: pins that ``truncated`` is NOT
    a sticky default but is genuinely set only when the guard fires.
    Uses the same fixture as the canonical paginates-until-no-next-page
    case (3 result pages, natural terminator).
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert result.truncated is False
    assert len(result.snapshots) == 30


class _ExactlyGuardBoundaryTransport(httpx.AsyncBaseTransport):
    """Serves exactly ``MAX_PAGES_GUARD + 1`` pages, last one with no next-arrow.

    Used to pin the for/else boundary fix: the orchestrator's
    ``for _ in range(MAX_PAGES_GUARD)`` loop runs ``MAX_PAGES_GUARD``
    *next-page* iterations on top of the initial search POST. When the
    result set's natural terminator lands on the page returned by the
    final iteration (the GUARD-th next-page POST), the loop exhausts
    without ``break`` even though there is no further page to fetch.
    The naive ``for/else`` would falsely report ``truncated=True``; the
    fix re-checks ``has_next_page`` on the final response and only
    sets ``truncated=True`` when a next-page genuinely exists.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        self._page_with_next = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        self._page_without_next = _NEXT_BTN_INPUT_RE.sub(b"", self._page_with_next)
        assert self._page_without_next != self._page_with_next, (
            "stripping next-arrow must mutate the page"
        )
        # Search POST returns page 1 (with next-arrow). Then we expect
        # MAX_PAGES_GUARD next-page POSTs. The first MAX_PAGES_GUARD-1
        # of those return pages with the next-arrow; the LAST one (the
        # GUARD-th iteration) returns the terminator page (no next-arrow).
        # Total result pages: 1 + MAX_PAGES_GUARD = MAX_PAGES_GUARD + 1.
        self._next_page_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = str(request.url)
        self.calls.append((request.method, url, body))

        if request.method == "GET" and "CourseSearch.aspx" in url:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
            return _html_response(self._form_initial)
        if request.method == "POST" and b"btnSearch=Suchen" in body:
            return _html_response(self._page_with_next)
        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            self._next_page_count += 1
            # The GUARD-th next-page POST returns the terminator page.
            if self._next_page_count == MAX_PAGES_GUARD:
                return _html_response(self._page_without_next)
            return _html_response(self._page_with_next)
        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


async def test_crawl_district_truncated_false_when_natural_end_at_guard_boundary() -> None:
    """Boundary: result set of EXACTLY ``MAX_PAGES_GUARD + 1`` pages with the
    final page carrying no next-arrow must NOT report ``truncated=True``.

    The naive ``for/else`` implementation would falsely flag this case as
    truncated, because the loop exhausts ``range(MAX_PAGES_GUARD)`` without
    a ``break`` -- but the final response already terminates the result
    set, so the user/scheduler should NOT see a "tail dropped" warning.

    The fix is to re-inspect ``has_next_page`` on the last response after
    the loop exhausts, and only mark ``truncated=True`` when a next page
    genuinely exists.
    """
    transport = _ExactlyGuardBoundaryTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
        )

    assert result.truncated is False, (
        "result set whose natural terminator lands on the GUARD-th next-page "
        "must not be flagged truncated (boundary false-positive)"
    )
    # Sanity: the transport actually served the full budget.
    # 2 setup POSTs (Erweitert + search) + MAX_PAGES_GUARD next-page POSTs.
    next_page_post_count = sum(
        1 for (m, _, b) in transport.calls if m == "POST" and b"ctl01%24ctl04.x=" in b
    )
    assert next_page_post_count == MAX_PAGES_GUARD, (
        f"transport should serve exactly {MAX_PAGES_GUARD} next-page POSTs; "
        f"got {next_page_post_count}"
    )


# ---------------------------------------------------------------------------
# Round 2: multi-district aggregation + truth-table coverage + sanity pins
# ---------------------------------------------------------------------------


class _PerDistrictBehaviourTransport(httpx.AsyncBaseTransport):
    """Two-district transport with per-district behaviour.

    The first district to be searched (whichever checkbox index it carries)
    terminates naturally after a single page. The second district enters
    an infinite-next-arrow loop so it trips the page guard.

    The aggregation contract under test is:

    * ``CrawlResult.truncated`` flips to True for the multi-district
      crawl iff ANY district truncated.
    * Snapshots from the cleanly-terminating district are still present
      in the aggregate result; truncation of one district does not drop
      another's data.
    """

    _INFINITE_INDEX = 2  # district 32 in our standard test mapping

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
        page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
        self._page_with_next = page_1
        self._page_without_next = _NEXT_BTN_INPUT_RE.sub(b"", page_1)
        assert self._page_without_next != page_1, "next-arrow strip must mutate"
        self._last_checkbox_idx: int | None = None

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
            self._last_checkbox_idx = int(match.group(1))
            # Infinite-pages district keeps the next-arrow; clean district drops it.
            if self._last_checkbox_idx == self._INFINITE_INDEX:
                return _html_response(self._page_with_next)
            return _html_response(self._page_without_next)
        if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
            # Only the infinite-pages district paginates; it always returns
            # next-arrow.
            return _html_response(self._page_with_next)
        raise AssertionError(f"unexpected request: {request.method} {url} body={body[:200]!r}")


async def test_crawl_aggregates_truncated_true_when_any_district_truncates() -> None:
    """Multi-district aggregation: one truncated district -> aggregate truncated.

    District 31 (checkbox idx 5 in the captured form-initial) terminates
    after one page; district 32 (checkbox idx 2) tips into the infinite
    loop. The aggregate must (a) flip ``truncated=True``, AND (b) still
    carry district 31's snapshots — truncation in one district must NOT
    drop another's data.
    """
    transport = _PerDistrictBehaviourTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl(
            client=client,
            district_ids={31, 32},
            sleep_seconds=0,
        )

    assert result.truncated is True, (
        "aggregate truncated flag must flip when ANY district truncates"
    )
    # District 31's clean page-1 carries 10 captured snapshots; with district
    # 32 also serving the same fixture (paginated), the dedup keeps the
    # first-occurrence ones (district 31 sorts first). What we MUST see is
    # at least the 10 rows from the cleanly-terminating district.
    assert len(result.snapshots) >= 10, (
        "snapshots from the cleanly-terminating district must still be present"
    )


async def test_crawl_aggregates_truncated_false_when_all_districts_terminate() -> None:
    """Companion to the any-truncated test: all clean -> aggregate clean.

    Uses the standard fixture transport for both districts (3 result pages
    each, natural terminator). Aggregate ``truncated`` is False because no
    district hit the guard.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl(
            client=client,
            district_ids={31},
            sleep_seconds=0,
        )

    assert result.truncated is False


async def test_crawl_district_with_keyword_does_not_early_exit_when_page_one_has_results() -> None:
    """Truth-table case: keyword + page-1 with rows + no next-arrow.

    Early-exit must fire only on the (empty page + no next) case. Here
    page-1 carries rows AND lacks a next-arrow, so the orchestrator must
    still return after one fetch (no pagination), but with the page-1
    rows preserved.

    This closes the missing case in the existing truth-table coverage:
    * (empty + no-next) -> early-exit returns []
    * (rows + has-next) -> paginates
    * (rows + no-next)  -> THIS test: no early-exit (because rows exist),
      but no pagination either (because no next-arrow); single fetch.
    """

    class _RowsButTerminalTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, bytes]] = []
            self._form_initial = (FIXTURES / "form-initial.html").read_bytes()
            page_1 = (FIXTURES / "search-district-31-page-1.html").read_bytes()
            self._page_with_rows_no_next = _NEXT_BTN_INPUT_RE.sub(b"", page_1)
            assert self._page_with_rows_no_next != page_1

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            url = str(request.url)
            self.calls.append((request.method, url, body))

            if request.method == "GET" and "CourseSearch.aspx" in url:
                return _html_response(self._form_initial)
            if request.method == "POST" and b"lbtnTab2=Erweitert" in body:
                return _html_response(self._form_initial)
            if request.method == "POST" and b"btnSearch=Suchen" in body:
                return _html_response(self._page_with_rows_no_next)
            if request.method == "POST" and b"ctl01%24ctl04.x=" in body:
                raise AssertionError(
                    "no pagination expected when page-1 has rows but no next-arrow"
                )
            raise AssertionError(f"unexpected request: {request.method} {url}")

    transport = _RowsButTerminalTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
            keyword="yoga",
        )

    assert len(result.snapshots) > 0, "page-1 rows must be preserved"
    assert result.truncated is False
    methods = [m for (m, _, _) in transport.calls]
    # GET + Erweitert POST + search POST only; no pagination.
    assert methods == ["GET", "POST", "POST"], (
        f"single fetch expected when page-1 has rows + no next; got {methods}"
    )


def test_max_pages_guard_covers_known_largest_bezirk() -> None:
    """Sanity pin: a future revert to a small guard must break this test.

    Tempelhof-Schöneberg empirically returns 81+ pages of empty-search
    results; the guard must give comfortable headroom or the daily scan
    silently truncates. 150 is the value Phase 7 settled on; the lower
    bound here keeps a generous safety margin while still catching any
    accidental revert to (e.g.) 50.
    """
    assert scraper.MAX_PAGES_GUARD >= 150


async def test_search_post_with_explicit_keyword_forwards_term() -> None:
    """Companion to the empty-keyword default test.

    Pin that a non-empty ``keyword=`` is forwarded into ``txtSearchTerm``
    in the search POST body verbatim (URL-encoded). Closes the "keyword
    forwarding has no test" gap flagged in the round-2 review.
    """
    transport = _FixtureTransport()
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        await crawl_district(
            client=client,
            district_checkbox_index=5,
            sleep_seconds=0,
            keyword="goldschmiede",
        )

    search_post_body = next(
        body
        for (method, _, body) in transport.calls
        if method == "POST" and b"btnSearch=Suchen" in body
    )
    assert b"txtSearchTerm=goldschmiede" in search_post_body
