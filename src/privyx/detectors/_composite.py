"""Several detectors run as one."""

from __future__ import annotations

import asyncio

from privyx.core import Context, Detection, Services, Span
from privyx.detectors._base import Detector


class CompositeDetector(Detector):
    """Run several detectors concurrently and pool their spans.

    Overlaps are left in place on purpose: the policy must see every span before
    the operator merges them.  Merging first could fold an allowed EMAIL into a
    longer span of a type ``strict`` drops, and the email would leak.  Only exact
    duplicates (two detectors finding the same entity at the same range) are
    collapsed, so audit counts are not doubled.  A failing detector fails the
    whole detection rather than silently skipping its spans.
    """

    def __init__(self, detectors: list[Detector]) -> None:
        super().__init__()
        self._detectors = detectors

    async def open(self, services: Services) -> None:
        """Open each detector; one that fails closes those opened before it."""
        opened: list[Detector] = []
        try:
            for detector in self._detectors:
                await detector.open(services)
                opened.append(detector)
        except BaseException:
            for detector in reversed(opened):
                await detector.close()
            raise

    async def close(self) -> None:
        for detector in reversed(self._detectors):
            await detector.close()

    async def detect(self, text: str, context: Context) -> Detection:
        results = await asyncio.gather(*(d.detect(text, context) for d in self._detectors))
        unique: dict[tuple[int, int, str], Span] = {}
        for result in results:
            for span in result.spans:
                unique.setdefault((span.start, span.end, span.entity_type), span)
        return Detection(spans=list(unique.values()), cacheable=all(r.cacheable for r in results))
