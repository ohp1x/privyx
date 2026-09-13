"""Policy protocol and base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.context import Context
from privyx.core.result import Detection


@runtime_checkable
class Policy(Protocol):
    """Protocol for policies that filter or enrich detections."""

    async def decide(self, detection: Detection, context: Context) -> Detection: ...


class BasePolicy(ABC):
    """Convenience base class for policies."""

    name: str = ""

    async def decide(self, detection: Detection, context: Context) -> Detection:
        return self._decide_sync(detection, context)

    @abstractmethod
    def _decide_sync(self, detection: Detection, context: Context) -> Detection: ...
