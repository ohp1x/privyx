"""Context words: a match counts only with one of them nearby."""

from __future__ import annotations

import re
from collections.abc import Collection

#: How many words on each side :func:`near` looks at, as Presidio does.
CONTEXT_RADIUS = 5

_WORD = re.compile(r"[^\W_]+")


def near(text: str, start: int, end: int, words: Collection[str]) -> bool:
    """Whether one of ``words`` (lower case, may be phrases) is within five words of the span."""
    before = _WORD.findall(text[max(0, start - 120) : start])[-CONTEXT_RADIUS:]
    after = _WORD.findall(text[end : end + 120])[:CONTEXT_RADIUS]
    window = f" {' '.join(before + after).lower()} "
    return any(f" {word} " in window for word in words)


# --- Registry ---------------------------------------------------------------

#: Words one of which must be near a built-in entity's match.
#: A detector's own ``context`` for the entity replaces these.
CONTEXT: dict[str, frozenset[str]] = {}
