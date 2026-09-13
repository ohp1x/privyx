"""Placeholder for Presidio-backed detector (optional dependency).

Install with ``pip install presidio-analyzer`` and enable via config:

.. code-block:: yaml

    detector:
      type: presidio
      language: en
"""

from __future__ import annotations

import asyncio
from typing import Any

from privyx.core.context import Context
from privyx.core.errors import DetectorError
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector


class PresidioDetector(BaseDetector):
    """Detector backed by Microsoft Presidio analyzer.

    The analyzer is imported lazily so that Presidio remains an optional
    dependency. Raises :class:`DetectorError` if Presidio is not installed.

    Note: Presidio's AnalyzerEngine is synchronous; this detector runs it in
    a thread pool to remain non-blocking in async contexts.
    """

    name = "presidio"

    def __init__(self, language: str = "en", **kwargs: Any) -> None:
        super().__init__()
        self._language = language
        self._kwargs = kwargs
        self._analyzer = None

    def _get_analyzer(self) -> Any:
        if self._analyzer is None:
            try:
                from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]

                self._analyzer = AnalyzerEngine(**self._kwargs)
            except ImportError as exc:  # pragma: no cover - optional dep
                raise DetectorError(
                    "Presidio is not installed. Install with `pip install presidio-analyzer`."
                ) from exc
        return self._analyzer

    def detect_sync(self, text: str, context: Context) -> Detection:
        analyzer = self._get_analyzer()
        results = analyzer.analyze(text=text, language=self._language)
        detection = Detection()
        for r in results:
            detection.add(r.start, r.end, r.entity_type, text[r.start : r.end])
        return detection

    async def detect(self, text: str, context: Context) -> Detection:
        """Run Presidio in a thread pool to keep the event loop unblocked."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.detect_sync, text, context)
