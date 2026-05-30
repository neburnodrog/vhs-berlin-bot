"""Shared test helpers — exposed as plain importable symbols, not pytest fixtures.

Helpers used across the ``tests/`` collection (``test_jobs.py``,
``test_handlers.py``, ``test_e2e.py``, ``test_scraper.py``) lived inlined
(with subtle divergences) in each of those files until the Phase 6 review
pass. They are now defined here once and re-exported so callers can do::

    from conftest import _FixtureTransport, _AsyncContextLock, _make_context

Pytest auto-loads ``conftest.py`` for the ``tests/`` collection root; no
``__init__.py`` is needed. We deliberately keep these as functions/classes
(not ``@pytest.fixture``s) because several call sites use them from
``async`` test bodies that build their own context per scan pass, and
threading the values through pytest's fixture graph adds noise without
buying isolation.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.db import Availability

FIXTURES = Path(__file__).parent / "fixtures"

_NEXT_BTN_INPUT_RE = re.compile(
    rb'<input[^>]*name="ctl00\$Content\$ILDataGrid1\$ctl01\$ctl04"[^>]*>',
    re.IGNORECASE,
)


def _html_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "text/html; charset=iso-8859-15"},
    )


class _FixtureTransport(httpx.AsyncBaseTransport):
    """Replays the captured 4-stage flow plus a synthetic "no more pages" terminator.

    Behaviour: GET form -> POST Erweitert -> POST search -> POST page-2 ->
    POST page-3-with-no-next-arrow. The transport keeps minimal internal
    state (request log + next-page counter) so a fresh instance is needed
    per scan pass.
    """

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


class _AsyncContextLock:
    """Minimal async-lock replacement.

    We can't use an ``asyncio.Lock`` directly without an event loop in
    sync test fixtures, and ``AsyncMock`` swallows ``async with`` — so we
    hand-roll a tiny one whose entry/exit counts we can assert on.
    """

    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _AsyncContextLock:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exit_count += 1


def set_last_availability(
    conn: sqlite3.Connection, *, kurs_id: int, availability: Availability
) -> None:
    """Overwrite the stored ``last_availability`` for one course.

    Test-only seam used by the e2e + db tests to force a belegt -> bookable
    transition on a course that the fixture reports as bookable. Lifted
    out of ad-hoc raw SQL so the type system (``Availability`` literal)
    can catch typos at call sites — a stuck typo would silently make
    ``classify()`` raise on every future scan.

    Lives in ``tests/conftest.py`` rather than ``src/vhsbot/db.py``
    because it is exclusively used by tests; production code never
    overwrites ``last_availability`` directly (it goes through
    ``upsert_seen_course``).
    """
    with conn:
        conn.execute(
            "UPDATE seen_courses SET last_availability = ? WHERE kurs_id = ?",
            (availability, kurs_id),
        )


def _make_context(
    *,
    settings: Settings,
    conn: sqlite3.Connection,
    client: Any = None,
    args: list[str] | None = None,
    db_lock: _AsyncContextLock | None = None,
) -> MagicMock:
    """Build a fake PTB ``ContextTypes.DEFAULT_TYPE`` for handler/job tests.

    ``args`` and ``db_lock`` are optional extensions used by the handler
    tests; the job tests don't touch them but ignoring them is harmless.
    """
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot_data = {
        BD_SETTINGS: settings,
        BD_DB: conn,
        BD_DB_LOCK: db_lock or _AsyncContextLock(),
        BD_CLIENT: client if client is not None else object(),
    }
    ctx.args = args or []
    ctx.user_data = {}
    return ctx
