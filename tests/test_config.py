"""Tests for ``Settings`` (Phase 9 added ``redact``)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from vhsbot.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:SECRETBOTTOKENVALUE",
        allowed_user_ids=frozenset({42}),
        scan_time=time(hour=8),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=tmp_path / "vhsbot.db",
        snapshot_dir=tmp_path / "snapshots",
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


def test_redact_replaces_bot_token_with_placeholder(settings: Settings) -> None:
    text = "Telegram returned: 123456:SECRETBOTTOKENVALUE is invalid"
    redacted = settings.redact(text)

    assert "123456:SECRETBOTTOKENVALUE" not in redacted
    assert "[redacted]" in redacted


def test_redact_handles_multiple_occurrences(settings: Settings) -> None:
    """A traceback may include the token more than once (e.g. URL + body)."""
    text = "url=https://api/123456:SECRETBOTTOKENVALUE body={'token': '123456:SECRETBOTTOKENVALUE'}"
    redacted = settings.redact(text)

    assert "123456:SECRETBOTTOKENVALUE" not in redacted
    assert redacted.count("[redacted]") == 2


def test_redact_returns_input_unchanged_when_token_absent(settings: Settings) -> None:
    text = "Some unrelated error message with no secrets in it"
    assert settings.redact(text) == text


def test_redact_does_not_match_partial_prefix_of_token(settings: Settings) -> None:
    """Substring replacement must require the full token, not a partial prefix.

    Real bot tokens are long enough that a prefix collision is astronomically
    unlikely, but pinning this protects against a future regression where
    someone "improves" ``redact`` to match shorter prefixes.
    """
    text = "Saw the token prefix 123456: in this message"
    redacted = settings.redact(text)

    # Prefix '123456:' alone must not trigger redaction.
    assert redacted == text
    assert "[redacted]" not in redacted


def test_redact_handles_empty_string(settings: Settings) -> None:
    assert settings.redact("") == ""
