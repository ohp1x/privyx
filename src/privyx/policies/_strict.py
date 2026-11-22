"""Strict policy — only allow specific entity types."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from privyx.core import BUILTIN_ENTITIES, Context, Detection
from privyx.policies._base import Policy


class StrictPolicy(Policy):
    """Only pass through detections whose entity type is in ``allowed``.

    An empty ``allowed`` means the built-in entities, opt-in ones included.
    """

    name = "strict"

    class Options(Policy.Options):
        allowed: list[str] = Field(default_factory=list)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._allowed = set(self.options.allowed) or BUILTIN_ENTITIES

    def decide_sync(self, detection: Detection, context: Context) -> Detection:
        filtered = Detection()
        for span in detection.spans:
            if span.entity_type in self._allowed:
                filtered.spans.append(span)
        return filtered

    def reason(self) -> str:
        listed = "the types in policy.allowed" if self.options.allowed else "the built-in types"
        return f"the {self.name} policy masks only {listed}"
