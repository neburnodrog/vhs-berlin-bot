"""Unicode-folded substring keyword matching.

Pure functions. The ``fold`` helper normalizes German text so umlauts,
Eszett, and inconsistent casing all collapse to a single canonical form;
``matches`` then runs OR-of-substring checks over title + course-number
and returns the input keywords that hit, de-duplicated, in input order.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from vhsbot.db import CourseSnapshot

_WS_RE = re.compile(r"\s+")


def fold(s: str) -> str:
    """Normalize ``s`` for case- and diacritic-insensitive substring matching.

    Steps: NFKD decomposition, strip combining marks, casefold (handles ß →
    ss), then collapse all whitespace runs to a single space and strip ends.
    Idempotent on its own output.
    """
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped.casefold()
    return _WS_RE.sub(" ", folded).strip()


def matches(course: CourseSnapshot, keywords: Iterable[str]) -> list[str]:
    """Return the keywords that match ``course``, in input order, de-duplicated.

    A keyword "matches" when its folded form appears as a substring of
    ``fold(title) + " " + fold(course_number)``. Empty / whitespace-only
    keywords are skipped. Original casing of the keyword is preserved in
    the returned list.
    """
    haystack = fold(course.title) + " " + fold(course.course_number)
    hits: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        folded_kw = fold(kw)
        if not folded_kw:
            continue
        if folded_kw in seen:
            continue
        if folded_kw in haystack:
            hits.append(kw)
            seen.add(folded_kw)
    return hits
