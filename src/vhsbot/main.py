"""Application bootstrap for vhs-berlin-bot.

Wires up the Settings, the SQLite connection, the shared
``httpx.AsyncClient`` (with the configured polite User-Agent), the
python-telegram-bot ``Application`` (with PTB's ``AIORateLimiter`` for
outbound throttling), and every handler from :mod:`vhsbot.handlers`.
Shared resources live on ``application.bot_data`` so handlers can fetch
them without globals; the keys are defined once in :mod:`vhsbot._app_state`
and shared with the handlers module to avoid string-literal drift.

**On-demand backfill design choice (Phase 4):** the ``/watch`` handler
awaits ``scraper.crawl`` synchronously and only then returns. The user
sees a typing indicator for up to ~60s. We picked this over the
fire-and-forget alternative because:

1. The bot is single-user; nobody else is waiting for handler capacity.
2. The blocking flow is materially simpler — no orphan-task lifetime
   management, no race against ``shutdown``.

Phase 5 will add the daily ``JobQueue.run_daily`` scan; that's the right
place to introduce asynchronous fan-out, not here.

**Rate limiting:** ``AIORateLimiter`` throttles *every* outbound request
by default — including the 15-message backfill burst. We don't pass any
custom per-call ``rate_limit_args``; PTB's defaults (~30 req/s overall,
~20/s per group) sit well inside Telegram's limits for our single-user
workload.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from telegram.ext import AIORateLimiter, Application

from vhsbot import db, handlers
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SETTINGS
from vhsbot.config import Settings, load_settings
from vhsbot.jobs import daily_scan

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Open the shared httpx client and DB connection after PTB starts."""
    settings: Settings = application.bot_data[BD_SETTINGS]
    client = httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    application.bot_data[BD_CLIENT] = client
    application.bot_data[BD_DB] = conn
    application.bot_data[BD_DB_LOCK] = asyncio.Lock()
    logger.info("vhs-berlin-bot ready (db=%s)", settings.db_path)


async def _post_shutdown(application: Application) -> None:
    """Cleanly close the httpx client and DB connection."""
    client: httpx.AsyncClient | None = application.bot_data.get(BD_CLIENT)
    if client is not None:
        await client.aclose()
    conn = application.bot_data.get(BD_DB)
    if conn is not None:
        conn.close()
    logger.info("vhs-berlin-bot stopped")


def run() -> None:
    """Script entry point — see ``[project.scripts]`` in pyproject.toml."""
    settings = load_settings()
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .rate_limiter(AIORateLimiter())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data[BD_SETTINGS] = settings
    handlers.register_handlers(application)

    # Schedule the daily scan. PTB v22's JobQueue.run_daily takes a
    # ``datetime.time``; attach the configured tz directly so APScheduler
    # interprets ``settings.scan_time`` as local wall-clock time.
    if application.job_queue is not None:  # pragma: no branch
        application.job_queue.run_daily(
            daily_scan,
            time=settings.scan_time.replace(tzinfo=settings.tz),
            name="vhsbot-daily-scan",
        )

    application.run_polling()


if __name__ == "__main__":  # pragma: no cover
    run()
