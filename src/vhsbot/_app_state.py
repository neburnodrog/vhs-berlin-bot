"""Shared ``application.bot_data`` keys + shared DB-lock helper.

Hoisted out of ``handlers.py`` so ``main.py`` + ``jobs.py`` can use the
same constants when wiring resources and avoid string-literal drift.
The leading underscore is a soft "internal" signal — nothing outside
the vhsbot package should reach into ``bot_data`` directly.

``locked_db`` is the single async-context manager every site uses to
acquire the shared ``asyncio.Lock`` and yield the sqlite connection.
Living here (rather than in ``handlers.py``) means ``jobs.py`` does not
import the handlers module just to reach a lock helper.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

BD_SETTINGS = "settings"
BD_DB = "db"
BD_DB_LOCK = "db_lock"
BD_CLIENT = "http_client"


@asynccontextmanager
async def locked_db(
    context: ContextTypes.DEFAULT_TYPE,
) -> AsyncIterator[sqlite3.Connection]:
    """Acquire the shared DB lock and yield the connection.

    Every site that touches sqlite goes through this helper. The lock is
    shallow — it does NOT cover the time spent awaiting the network in
    callers — but it does serialise the writes themselves, which is what
    the Phase 1 plan promised.
    """
    lock = context.bot_data[BD_DB_LOCK]
    async with lock:
        yield context.bot_data[BD_DB]
