"""Result types returned by detectors and operators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Tie-break order when overlapping spans have the same length.  More specific
#: entity types win over broader ones: a 16-digit run that both PHONE and
#: CREDIT_CARD claim should be labelled CREDIT_CARD.  Types absent from this
#: table sort last (i.e. they lose to every listed type).
ENTITY_PRIORITY: dict[str, int] = {
    "SSN": 100,
    "CREDIT_CARD": 90,
    "EMAIL": 80,
    "IP_ADDRESS": 70,
    "PHONE": 60,
    "ZIPCODE": 10,
}


def _priority(entity_type: str) -> int:
    return ENTITY_PRIORITY.get(entity_type, 0)


def _dominant(left: Span, right: Span) -> Span:
    """Return whichever span should give the merged span its entity type."""
    if right.length != left.length:
        return right if right.length > left.length else left
    if _priority(right.entity_type) != _priority(left.entity_type):
        return right if _priority(right.entity_type) > _priority(left.entity_type) else left
    return left


@dataclass(slots=True)
class Span:
    """A detected span of sensitive text within a source string."""

    start: int
    end: int
    entity_type: str
    text: str

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(slots=True)
class Detection:
    """A detection produced by a detector.

    ``cacheable`` is false for a stand-in result, such as the LLM detector's
    regex-only fallback, so a detection cache never keeps it in place of a
    real scan.
    """

    spans: list[Span] = field(default_factory=list)
    cacheable: bool = True

    def add(self, start: int, end: int, entity_type: str, text: str) -> None:
        self.spans.append(Span(start, end, entity_type, text))

    def merged(self, source: str | None = None) -> Detection:
        """Return a new detection with overlapping/nested spans merged.

        When two detectors claim overlapping ranges (e.g. PHONE and
        CREDIT_CARD both matching ``4111 1111 1111 1111``), the merged span
        covers the union of both ranges and adopts the entity type of the
        *dominant* span — the longest one, tie-broken by :data:`ENTITY_PRIORITY`.

        Args:
            source: The text the spans were detected in.  Strongly recommended:
                it lets the merged span carry the exact substring it covers.
                Without it, the text is reconstructed from the overlapping
                spans, which assumes every ``span.text`` equals
                ``source[span.start:span.end]``.

        Returns:
            A new :class:`Detection` whose spans are disjoint and ordered.
        """
        ordered = sorted(self.spans, key=lambda s: (s.start, -s.end))
        merged: list[Span] = []
        for span in ordered:
            if not merged or span.start >= merged[-1].end:
                merged.append(span)
                continue

            prev = merged[-1]
            start = prev.start
            end = max(prev.end, span.end)
            dominant = _dominant(prev, span)

            if source is not None:
                text = source[start:end]
            elif end > prev.end:
                # Extend with the part of `span` that reaches past `prev`.
                text = prev.text + span.text[prev.end - span.start :]
            else:
                text = prev.text

            merged[-1] = Span(
                start=start,
                end=end,
                entity_type=dominant.entity_type,
                text=text,
            )
        return Detection(spans=merged)

    def __bool__(self) -> bool:
        return bool(self.spans)


@dataclass(slots=True)
class Transformation:
    """A single replacement applied to a source string."""

    start: int
    end: int
    original: str
    replacement: str


@dataclass(slots=True)
class TransformResult:
    """Result of a pseudonymization/deanonymization operation."""

    text: str
    transformations: list[Transformation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "transformations": [
                {
                    "start": t.start,
                    "end": t.end,
                    "original": t.original,
                    "replacement": t.replacement,
                }
                for t in self.transformations
            ],
        }
