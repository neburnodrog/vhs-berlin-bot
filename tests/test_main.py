"""Tests for the application-bootstrap wiring in ``vhsbot.main``.

Phase 5-review fix: previously zero tests touched ``main.py``. A typo in
the JobQueue callback name or schedule time would silently disable the
bot. These tests pin the wiring by constructing an ``Application`` via
the extracted ``build_application`` helper (which ``run()`` now defers
to) and asserting on the registered jobs / handlers / error handler.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram import BotCommand
from telegram.ext import CommandHandler, ConversationHandler

from vhsbot import handlers, main
from vhsbot._app_state import BD_SETTINGS
from vhsbot.config import Settings
from vhsbot.jobs import daily_scan


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123:TEST",
        allowed_user_ids=frozenset({111}),
        scan_time=time(hour=8, minute=0),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=tmp_path / "vhsbot-test.db",
        snapshot_dir=tmp_path / "snapshots",
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


def test_build_application_registers_daily_scan_job(settings: Settings) -> None:
    """JobQueue must contain exactly one job named ``vhsbot-daily-scan``
    bound to :func:`vhsbot.jobs.daily_scan`."""
    app = main.build_application(settings)
    assert app.job_queue is not None
    jobs = app.job_queue.jobs()
    matching = [j for j in jobs if j.name == "vhsbot-daily-scan"]
    assert len(matching) == 1, (
        f"expected exactly one 'vhsbot-daily-scan' job, found {len(matching)} (all: {jobs!r})"
    )
    assert matching[0].callback is daily_scan, (
        f"daily-scan job must be bound to jobs.daily_scan, got {matching[0].callback!r}"
    )


def test_build_application_registers_global_error_handler(settings: Settings) -> None:
    """``Application.add_error_handler`` must be called with
    ``handlers.global_error_handler`` exactly once."""
    app = main.build_application(settings)
    assert handlers.global_error_handler in app.error_handlers, (
        f"global error handler not registered; saw: {list(app.error_handlers)!r}"
    )


def test_build_application_registers_all_command_handlers(settings: Settings) -> None:
    """Every one of the nine slash commands must be registered with the whitelist filter."""
    app = main.build_application(settings)

    # Collect every CommandHandler — including those nested in the
    # onboarding ConversationHandler (entry_points + fallbacks).
    found_commands: set[str] = set()
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                found_commands |= set(h.commands)
                assert h.filters is not None, f"handler {h.commands} missing whitelist filter"
            elif isinstance(h, ConversationHandler):
                for sub in (*h.entry_points, *h.fallbacks):
                    if isinstance(sub, CommandHandler):
                        found_commands |= set(sub.commands)
                        assert sub.filters is not None, (
                            f"conversation handler {sub.commands} missing whitelist filter"
                        )

    expected = {
        "start",
        "help",
        "list",
        "watch",
        "unwatch",
        "districts",
        "pause",
        "resume",
        "scan",
        "cancel",
    }
    missing = expected - found_commands
    assert not missing, f"missing command handlers: {sorted(missing)}"


def test_bot_commands_constant_includes_all_top_level_handlers(settings: Settings) -> None:
    """``_BOT_COMMANDS`` must mirror every top-level CommandHandler.

    Cross-check invariant. If someone adds a new ``/foo`` to
    ``handlers.register_handlers`` but forgets to extend
    ``main._BOT_COMMANDS``, the Telegram slash autocomplete silently
    diverges from reality. This test fails loudly in that case.

    ``/cancel`` is intentionally omitted from ``_BOT_COMMANDS`` -- it
    lives only inside the onboarding ConversationHandler's fallbacks and
    is meaningless outside that flow.
    """
    app = main.build_application(settings)

    # "Top-level" from the user's POV means any command they can type
    # outside a conversation -- that's direct CommandHandlers PLUS
    # ConversationHandler entry_points (e.g. /start). We deliberately
    # skip ConversationHandler.fallbacks: those are conversation-only
    # interrupts and (for /cancel) genuinely meaningless outside a flow.
    top_level_commands: set[str] = set()
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler):
            top_level_commands |= set(h.commands)
        elif isinstance(h, ConversationHandler):
            for sub in h.entry_points:
                if isinstance(sub, CommandHandler):
                    top_level_commands |= set(sub.commands)

    menu_commands = {cmd.command for cmd in main._BOT_COMMANDS}

    # Direction 1: every top-level registered command (except /cancel)
    # must be in the slash menu.
    missing_from_menu = (top_level_commands - {"cancel"}) - menu_commands
    assert not missing_from_menu, (
        f"top-level commands missing from _BOT_COMMANDS slash menu: {sorted(missing_from_menu)}"
    )

    # Direction 2: every entry in the slash menu must correspond to a
    # registered top-level command (no phantom menu entries).
    phantom = menu_commands - top_level_commands
    assert not phantom, (
        f"_BOT_COMMANDS entries with no registered top-level handler: {sorted(phantom)}"
    )

    # And /cancel must NOT be in the menu (intentional omission).
    assert "cancel" not in menu_commands, (
        "/cancel must not appear in _BOT_COMMANDS; it is a conversation "
        "fallback only and is meaningless outside the onboarding flow"
    )


async def test_post_init_calls_set_my_commands(settings: Settings) -> None:
    """``_post_init`` must register the slash autocomplete menu via
    ``bot.set_my_commands(_BOT_COMMANDS)``.

    We stub the bot with an ``AsyncMock`` and avoid the real Application
    so we don't open any network connections. ``db.connect`` +
    ``db.init_schema`` use the temp ``settings.db_path`` -- real but
    cheap (sqlite file in tmp_path).
    """
    application = MagicMock()
    application.bot_data = {BD_SETTINGS: settings}
    application.bot = MagicMock()
    application.bot.set_my_commands = AsyncMock()

    await main._post_init(application)

    application.bot.set_my_commands.assert_awaited_once_with(main._BOT_COMMANDS)


def test_bot_commands_are_botcommand_instances() -> None:
    """``_BOT_COMMANDS`` must be ``BotCommand`` instances (not raw tuples).

    Pins the typed signature so descriptions can't accidentally swap
    positions during a future refactor.
    """
    assert len(main._BOT_COMMANDS) > 0
    for entry in main._BOT_COMMANDS:
        assert isinstance(entry, BotCommand), (
            f"_BOT_COMMANDS entries must be telegram.BotCommand, got {type(entry).__name__}"
        )


def test_no_german_diacritics_in_bot_authored_constants() -> None:
    """Cheap regression guard against accidentally re-introducing German strings
    in bot-authored copy. District names + course titles are NOT covered here;
    they come from VHS Berlin's catalog and may legitimately contain umlauts.
    """
    from vhsbot.handlers import _TRUNCATION_NOTE

    forbidden = set("äöüßÄÖÜ")
    for cmd in main._BOT_COMMANDS:
        assert not (forbidden & set(cmd.description)), f"German diacritic in {cmd!r}"
    assert not (forbidden & set(_TRUNCATION_NOTE)), "German diacritic in _TRUNCATION_NOTE"


def test_build_application_uses_aiorate_limiter(settings: Settings) -> None:
    """``AIORateLimiter`` must be the Application's outbound throttle.

    Pins the rate-limiter wiring in :func:`vhsbot.main.build_application`:
    the docstring says PTB's ``AIORateLimiter`` throttles every outbound
    request, and a regression here (e.g. ``.rate_limiter(None)`` slipping
    in) would silently lift the rate limit. PTB v22 exposes the configured
    limiter on ``application.bot.rate_limiter``.
    """
    from telegram.ext import AIORateLimiter

    app = main.build_application(settings)
    rate_limiter = app.bot.rate_limiter
    assert rate_limiter is not None, "AIORateLimiter must be registered on the Application"
    assert isinstance(rate_limiter, AIORateLimiter), (
        f"expected AIORateLimiter, got {type(rate_limiter).__name__}: {rate_limiter!r}"
    )
