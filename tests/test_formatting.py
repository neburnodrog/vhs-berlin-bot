"""Tests for the canonical course_card formatter (Phase 5)."""

from __future__ import annotations

from typing import Any

from vhsbot.db import CourseSnapshot
from vhsbot.formatting import course_card


def _course(**kw: Any) -> CourseSnapshot:
    base: dict[str, Any] = {
        "kurs_id": 12345,
        "title": "Yoga sanft",
        "course_number": "Mi251-001K",
        "district": "Mitte",
        "venue": None,
        "date_range": "01.06.2026 - 30.07.2026",
        "availability": ">2",
    }
    base.update(kw)
    return CourseSnapshot(**base)


# ---------------------------------------------------------------------------
# Reason prefix tests
# ---------------------------------------------------------------------------


def test_course_card_includes_reason_prefix_for_new() -> None:
    text, _ = course_card(
        _course(),
        matched_keywords=["Yoga"],
        reason="new",
        detail_url="https://example.test/x",
    )
    assert "New course matching" in text
    # The matched keyword should appear in the prefix line.
    assert "Yoga" in text


def test_course_card_includes_reason_prefix_for_back_in_stock() -> None:
    text, _ = course_card(
        _course(),
        matched_keywords=["Yoga"],
        reason="back_in_stock",
        detail_url="https://example.test/x",
    )
    assert "Back in stock" in text


def test_course_card_includes_reason_prefix_for_backfill() -> None:
    text, _ = course_card(
        _course(),
        matched_keywords=["Yoga"],
        reason="backfill",
        detail_url="https://example.test/x",
    )
    assert "backfill" in text.lower()
    assert "Match from your watch backfill" in text


# ---------------------------------------------------------------------------
# Markdown V2 escaping
# ---------------------------------------------------------------------------


def test_course_card_escapes_markdown_v2_in_title() -> None:
    # A title chock-full of MD-V2 reserved characters must come out
    # backslash-escaped so Telegram does not interpret them.
    bad_title = "Yoga * _ [ ] ( ) ~ ` > # + - = | { } . ! v2"
    text, _ = course_card(
        _course(title=bad_title),
        matched_keywords=["Yoga"],
        reason="new",
        detail_url="https://example.test/x",
    )
    # Every reserved char must appear backslash-prefixed somewhere in
    # the rendered text — sample a handful that are highly characteristic.
    for ch in ("*", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", ".", "!"):
        assert f"\\{ch}" in text, f"reserved char {ch!r} not escaped in output"


# ---------------------------------------------------------------------------
# Inline keyboard button
# ---------------------------------------------------------------------------


def test_course_card_button_links_to_detail_url() -> None:
    detail_url = "https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseDetail.aspx?id=12345"
    _, markup = course_card(
        _course(),
        matched_keywords=["Yoga"],
        reason="new",
        detail_url=detail_url,
    )
    rows = markup.inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert rows[0][0].url == detail_url


# ---------------------------------------------------------------------------
# Phase 6 addition: minimal course data
# ---------------------------------------------------------------------------


def test_course_card_with_minimal_course_data() -> None:
    """A ``CourseSnapshot`` with ``district=None`` and ``date_range=None``
    must format without crashing.

    Pins the optional-field branches in :func:`course_card`: the District/
    Date lines must be omitted when their values are None, rather than
    rendering "District: None" or raising. The Seats + title + course-number
    lines remain because they are non-optional in CourseSnapshot.
    """
    minimal = _course(district=None, date_range=None)
    text, markup = course_card(
        minimal,
        matched_keywords=["Yoga"],
        reason="new",
        detail_url="https://example.test/x",
    )

    assert isinstance(text, str)
    assert text  # non-empty
    # Required fields still in output.
    assert "Yoga sanft" in text or "Yoga" in text
    # Optional-field labels must NOT leak with a "None" string.
    assert "District: None" not in text
    assert "Date: None" not in text
    # Empty-string for the missing fields must also not produce blank label lines.
    assert "District: \n" not in text
    assert "Date: \n" not in text
    # The keyboard still resolves cleanly.
    assert markup.inline_keyboard[0][0].url == "https://example.test/x"


# ---------------------------------------------------------------------------
# Phase 9: format_failure_alert
# ---------------------------------------------------------------------------


def _settings_for_failure_alert(token: str = "123:SECRETTOKEN") -> Any:
    """Build a minimal Settings instance for failure-alert tests."""
    from datetime import time
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from vhsbot.config import Settings

    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=frozenset({111}),
        scan_time=time(hour=8),
        tz=ZoneInfo("Europe/Berlin"),
        db_path=Path("/tmp/x.db"),
        snapshot_dir=Path("/tmp/x-snap"),
        log_level="INFO",
        scrape_sleep_seconds=0.0,
    )


def test_format_failure_alert_includes_exception_type_and_time() -> None:
    from datetime import datetime

    from vhsbot.formatting import format_failure_alert

    settings = _settings_for_failure_alert()
    exc = RuntimeError("VHS Berlin returned 503")
    when = datetime(2026, 6, 5, 8, 1, 42)

    text = format_failure_alert(exc, settings, when)

    assert "RuntimeError" in text
    assert "503" in text
    assert "08:01" in text


def test_format_failure_alert_redacts_bot_token() -> None:
    """Critical fix #3: the bot token must never appear in the alert body.

    Telegram messages are cached client-side; a token in chat history is
    effectively leaked.
    """
    from datetime import datetime

    from vhsbot.formatting import format_failure_alert

    token = "987654:VERYSECRETTOKEN"
    settings = _settings_for_failure_alert(token=token)
    # An exception whose str() embeds the token (e.g. httpx URL formatting).
    exc = RuntimeError(f"Auth failed: token={token} in request")
    when = datetime(2026, 6, 5, 8, 1, 0)

    text = format_failure_alert(exc, settings, when)

    assert token not in text, "format_failure_alert must redact the bot token"
    assert "[redacted]" in text


def test_format_failure_alert_truncates_long_exception_messages() -> None:
    """The redacted detail is truncated to 200 chars so a giant traceback can't
    blow up the Telegram message-size budget.
    """
    from datetime import datetime

    from vhsbot.formatting import format_failure_alert

    settings = _settings_for_failure_alert()
    long_payload = "x" * 1000
    exc = RuntimeError(long_payload)
    when = datetime(2026, 6, 5, 8, 0, 0)

    text = format_failure_alert(exc, settings, when)

    # The total text is the prefix ("⚠️ Scan failed at HH:MM: RuntimeError: ")
    # plus the 200-char truncated detail. Easier to assert: the resulting
    # text is bounded well under 400 chars.
    assert len(text) < 400, (
        f"format_failure_alert must truncate; got {len(text)}-char body for 1000-char exc"
    )
