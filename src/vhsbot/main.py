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

The Phase 5 daily ``JobQueue.run_daily`` scan + manual ``/scan`` trigger
share the same ``daily_scan`` callback; manual triggers use a
``scan_running`` bot_data flag so a manual call during the scheduled
window correctly defers.

**Rate limiting:** ``AIORateLimiter`` throttles *every* outbound request
by default — including the 15-message backfill burst. We don't pass any
custom per-call ``rate_limit_args``; PTB's defaults (~30 req/s overall,
~20/s per group) sit well inside Telegram's limits for our single-user
workload.

``run()`` is the script entry point; the testable surface is
:func:`build_application`, which assembles the ``Application`` with all
handlers + the daily-scan job registered but does NOT call
``run_polling``. Tests construct an Application via ``build_application``
and introspect ``app.job_queue.jobs()`` + ``app.error_handlers`` +
``app.handlers`` without ever opening a real Telegram connection.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from telegram import BotCommand
from telegram.ext import AIORateLimiter, Application

from vhsbot import db, handlers
from vhsbot._app_state import BD_CLIENT, BD_DB, BD_DB_LOCK, BD_SCAN_RUNNING, BD_SETTINGS
from vhsbot.config import Settings, load_settings
from vhsbot.jobs import daily_scan

logger = logging.getLogger(__name__)


# Slash-autocomplete menu populated via setMyCommands at startup.
# Order is the order the user sees in Telegram's command list.
# /cancel is intentionally omitted — it's only a ConversationHandler fallback,
# meaningful inside the onboarding flow only.
_BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "Start the bot / show greeting"),
    BotCommand("help", "Show help"),
    BotCommand("list", "List my watched keywords"),
    BotCommand("watch", "Watch a keyword (runs backfill)"),
    BotCommand("unwatch", "Stop watching a keyword"),
    BotCommand("districts", "Change my districts"),
    BotCommand("pause", "Pause notifications"),
    BotCommand("resume", "Resume notifications"),
    BotCommand("scan", "Scan now"),
    BotCommand("status", "Show last scan status"),
)


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
    # Manual /scan + scheduled daily_scan concurrency guard. Both sides
    # set the flag on entry and reset it in their finally clauses; see
    # handlers.scan + jobs.daily_scan.
    application.bot_data[BD_SCAN_RUNNING] = False
    # Populate the Telegram client's "/" slash-autocomplete menu. Sent once
    # per startup; Telegram caches it server-side per (bot, scope, language).
    await application.bot.set_my_commands(_BOT_COMMANDS)
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


def build_application(settings: Settings) -> Application:
    """Assemble + return a fully-wired PTB ``Application``.

    Does NOT call ``run_polling()`` — that's the script entrypoint's
    job. Exposed so tests can introspect the registered job-queue jobs,
    error handlers, and command handlers without standing up a real
    Telegram connection. The two are kept separate so a typo in the
    daily-scan callback name (or schedule time) is caught by a unit
    test rather than discovered in production.
    """
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

    return application


def run() -> None:
    """Script entry point — see ``[project.scripts]`` in pyproject.toml."""
    settings = load_settings()
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )

    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":  # pragma: no cover
    run()
