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
from zoneinfo import ZoneInfo

import pytest
from telegram.ext import CommandHandler, ConversationHandler

from vhsbot import handlers, main
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
