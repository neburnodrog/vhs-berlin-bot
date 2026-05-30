"""Classify a course snapshot against its previously-seen state.

Pure logic. Drives the notification policy locked in ``tasks/todo .md``:
notify on *new* courses and on *belegt → bookable* transitions; stay
silent on everything else.
"""

from __future__ import annotations

from typing import Literal

from vhsbot.db import CourseSnapshot, SeenCourse

ClassifyResult = Literal["new", "back_in_stock", "unchanged", "still_full"]

_BOOKABLE: frozenset[str] = frozenset({">2", "2", "1"})
_VALID_AVAILABILITY: frozenset[str] = frozenset({">2", "2", "1", "belegt"})


def classify(current: CourseSnapshot, previous: SeenCourse | None) -> ClassifyResult:
    """Return the notification-policy bucket for ``current`` vs ``previous``.

    - ``"new"``: ``previous is None`` (course we have never seen before).
    - ``"back_in_stock"``: previous availability was ``"belegt"`` and current
      is one of ``">2"``, ``"2"``, ``"1"``.
    - ``"still_full"``: previous and current both ``"belegt"``.
    - ``"unchanged"``: every other combination of the four valid literals,
      including going-out-of-stock (bookable → belegt), which is explicitly
      not notified per the locked policy.

    Raises :class:`ValueError` if ``current.availability`` is not one of the
    four literals emitted by :func:`vhsbot.parser._availability` — this is
    a defensive guard so parser drift surfaces here rather than silently
    mis-classifying.
    """
    if current.availability not in _VALID_AVAILABILITY:
        raise ValueError(
            f"unknown availability literal {current.availability!r}; "
            f"expected one of {sorted(_VALID_AVAILABILITY)}"
        )
    if previous is None:
        return "new"
    if previous.last_availability == "belegt":
        if current.availability in _BOOKABLE:
            return "back_in_stock"
        if current.availability == "belegt":
            return "still_full"
    return "unchanged"
