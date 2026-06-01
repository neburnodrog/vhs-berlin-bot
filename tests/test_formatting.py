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
