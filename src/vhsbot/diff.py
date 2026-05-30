"""Classify a course snapshot against its previously-seen state.

Pure logic. Drives the notification policy locked in ``tasks/todo .md``:
notify on *new* courses and on *belegt → bookable* transitions; stay
silent on everything else.
"""

from __future__ import annotations

from typing import Literal

from vhsbot.db import AVAILABILITY_LITERALS, BOOKABLE_AVAILABILITY, CourseSnapshot, SeenCourse

ClassifyResult = Literal["new", "back_in_stock", "unchanged", "still_full"]


def classify(current: CourseSnapshot, previous: SeenCourse | None) -> ClassifyResult:
    """Return the notification-policy bucket for ``current`` vs ``previous``.

    - ``"new"``: ``previous is None`` (course we have never seen before).
    - ``"back_in_stock"``: previous availability was ``"belegt"`` and current
      is one of ``">2"``, ``"2"``, ``"1"``.
    - ``"still_full"``: previous and current both ``"belegt"``.
    - ``"unchanged"``: every other combination of the four valid literals,
      including going-out-of-stock (bookable → belegt), which is explicitly
      not notified per the locked policy.

    Raises :class:`ValueError` if ``current.availability`` or
    ``previous.last_availability`` is not one of the four literals in
    :data:`vhsbot.db.AVAILABILITY_LITERALS` — symmetric defensive guard so
    parser drift OR stored-state drift surfaces here rather than silently
    mis-classifying.
    """
    if current.availability not in AVAILABILITY_LITERALS:
        raise ValueError(
            f"unknown availability literal {current.availability!r}; "
            f"expected one of {sorted(AVAILABILITY_LITERALS)}"
        )
    if previous is None:
        return "new"
    if previous.last_availability not in AVAILABILITY_LITERALS:
        raise ValueError(
            f"unknown previous.last_availability: {previous.last_availability!r}; "
            f"expected one of {sorted(AVAILABILITY_LITERALS)}"
        )
    if previous.last_availability == "belegt":
        if current.availability in BOOKABLE_AVAILABILITY:
            return "back_in_stock"
        if current.availability == "belegt":
            return "still_full"
    return "unchanged"
