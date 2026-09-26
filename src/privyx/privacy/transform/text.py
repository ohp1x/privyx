"""Text transformation helpers for the privacy engine."""

from __future__ import annotations

from collections.abc import Callable

from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session


def apply_replacements(text: str, transformations: list[Transformation]) -> str:
    """Apply a list of transformations to ``text`` in order (must be sorted)."""
    result = text
    for t in transformations:
        result = result[: t.start] + t.replacement + result[t.end :]
    return result


def replace_spans(
    text: str,
    detection: Detection,
    replacement_for: Callable[[str, str], str],
    session: Session,
) -> TransformResult:
    """Replace each span using ``replacement_for(entity_type, original)``.

    Applies replacements right-to-left so earlier offsets stay valid.
    """
    detection = detection.merged()
    transforms: list[Transformation] = []
    result = text
    for span in reversed(detection.spans):
        replacement = replacement_for(span.entity_type, span.text)
        result = result[: span.start] + replacement + result[span.end :]
        transforms.append(Transformation(span.start, span.end, span.text, replacement))
    transforms.reverse()
    return TransformResult(text=result, transformations=transforms)
