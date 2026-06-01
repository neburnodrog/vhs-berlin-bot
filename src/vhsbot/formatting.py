"""Notification-message formatting.

A single canonical ``course_card`` function builds the Markdown V2 text +
inline keyboard for every course notification the bot sends, whether
from the daily scan (``reason="new"`` / ``"back_in_stock"``) or the
on-demand backfill (``reason="backfill"``). The reason drives a small
prefix line so the user can tell at a glance why the message arrived.

Every user-influenced field is escaped via PTB's
``telegram.helpers.escape_markdown(version=2)``. No hand-rolled escaper.
"""

from __future__ import annotations

from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from vhsbot.db import CourseSnapshot

CardReason = Literal["new", "back_in_stock", "backfill"]

_REASON_PREFIX: dict[CardReason, str] = {
    "new": "New course matching: {kw}",
    "back_in_stock": "Back in stock: {kw}",
    "backfill": "Match from your watch backfill: {kw}",
}


def _esc(s: str) -> str:
    return escape_markdown(s, version=2)


def course_card(
    course: CourseSnapshot,
    matched_keywords: list[str],
    reason: CardReason,
    detail_url: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the notification text + inline keyboard for one course.

    ``reason`` drives the leading prefix line:

    - ``"new"``         -> "New course matching: ..."
    - ``"back_in_stock"`` -> "Back in stock: ..."
    - ``"backfill"``    -> "Match from your watch backfill: ..."

    The inline keyboard always carries a single "Open details" button
    pointing at ``detail_url``.

    A runtime ``assert`` catches the case where a caller bypasses the
    type-checker (e.g. ``cast()`` from a wider ``str``) and passes a
    reason outside the literal set — the prefix table KeyError would
    otherwise be opaque.
    """
    assert reason in {"new", "back_in_stock", "backfill"}, (
        f"course_card called with unknown reason {reason!r}; "
        f"expected one of new, back_in_stock, backfill"
    )
    matched_raw = ", ".join(matched_keywords) if matched_keywords else ""
    prefix = _esc(_REASON_PREFIX[reason].format(kw=matched_raw))

    title = _esc(course.title)
    cnum = _esc(course.course_number)
    district = _esc(course.district or "")
    date_range = _esc(course.date_range or "")
    avail = _esc(course.availability)

    parts: list[str] = [prefix, f"*{title}*", f"Course: `{cnum}`"]
    if district:
        parts.append(f"District: {district}")
    if date_range:
        parts.append(f"Date: {date_range}")
    parts.append(f"Seats: {avail}")

    text = "\n".join(parts)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open details", url=detail_url)]])
    return text, markup
