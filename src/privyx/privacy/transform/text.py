"""Text transformation helpers for the privacy engine."""

from __future__ import annotations

from privyx.core.result import Transformation


def apply_replacements(text: str, transformations: list[Transformation]) -> str:
    """Return ``text`` with each transformation's ``[start, end)`` replaced.

    ``transformations`` hold offsets into ``text``, sorted by ``start`` and not
    overlapping.  The pieces are joined once, so the cost is linear: rebuilding
    the whole string per span made thousands of spans quadratic.
    """
    parts: list[str] = []
    last = 0
    for t in transformations:
        parts += (text[last : t.start], t.replacement)
        last = t.end
    parts.append(text[last:])
    return "".join(parts)
