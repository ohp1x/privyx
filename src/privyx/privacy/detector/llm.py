"""LLM-assisted detector (optional).

Uses a configured LLM client to detect sensitive spans. Because this adds
latency and cost, it should only be enabled explicitly in configuration.
"""

from __future__ import annotations

from typing import Protocol

from privyx.core.context import Context
from privyx.core.errors import DetectorError
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


class LLMDetector(BaseDetector):
    """Detect sensitive spans by asking an LLM.

    Args:
        client: Async client with a ``complete(prompt) -> str`` method.
        instructions: Optional system/instructions text appended to the prompt.
    """

    name = "llm"

    def __init__(self, client: LLMClient, instructions: str | None = None) -> None:
        super().__init__()
        self._client = client
        self._instructions = instructions or (
            "List every span of personal or sensitive data as "
            "JSON array of [start, end, entity_type, text] tuples."
        )

    async def detect(self, text: str, context: Context) -> Detection:
        try:
            response = await self._client.complete(
                f"{self._instructions}\n\nTEXT:\n{text}"
            )
        except Exception as exc:  # pragma: no cover - external dependency
            raise DetectorError(f"LLM detector failed: {exc}") from exc
        return _parse_spans(response, text)


def _parse_spans(response: str, text: str) -> Detection:
    """Best-effort parse of an LLM JSON response into a Detection."""
    import json

    detection = Detection()
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return detection
    if not isinstance(payload, list):
        return detection
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            start, end, entity = int(item[0]), int(item[1]), str(item[2])
        except (TypeError, ValueError):
            continue
        span_text = text[start:end]
        if span_text:
            detection.add(start, end, entity, span_text)
    return detection
