"""Text transformation helpers for the privacy engine."""

from __future__ import annotations

from collections.abc import Callable

from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session


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


def replace_spans(
    text: str,
    detection: Detection,
    replacement_for: Callable[[str, str], str],
    session: Session,
) -> TransformResult:
    """Replace each span using ``replacement_for(entity_type, original)``."""
    detection = detection.merged()
    transforms: list[Transformation] = []
    for span in detection.spans:
        replacement = replacement_for(span.entity_type, span.text)
        transforms.append(Transformation(span.start, span.end, span.text, replacement))
    return TransformResult(text=apply_replacements(text, transforms), transformations=transforms)
