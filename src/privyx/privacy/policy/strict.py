"""Strict policy — only allow specific entity types."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.policy.base import BasePolicy


class StrictPolicy(BasePolicy):
    """Only pass through detections whose entity type is in ``allowed_types``.

    Args:
        allowed_types: Set of entity type strings to keep.
    """

    name = "strict"

    def __init__(self, allowed_types: set[str] | None = None) -> None:
        super().__init__()
        self._allowed = allowed_types or {"EMAIL", "PHONE", "SSN", "CREDIT_CARD", "IP_ADDRESS"}

    def _decide_sync(self, detection: Detection, context: Context) -> Detection:
        filtered = Detection()
        for span in detection.spans:
            if span.entity_type in self._allowed:
                filtered.spans.append(span)
        return filtered
