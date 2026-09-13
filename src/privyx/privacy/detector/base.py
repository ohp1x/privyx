"""Detector base classes and the Detector protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.context import Context
from privyx.core.result import Detection


@runtime_checkable
class Detector(Protocol):
    """Protocol implemented by all entity detectors."""

    name: str

    async def detect(self, text: str, context: Context) -> Detection: ...


class BaseDetector(ABC):
    """Convenience base class for detectors.

    Subclasses must implement :meth:`detect_sync` (or :meth:`detect`). The base
    provides a ``name`` derived from the class name unless overridden.
    """

    name: str = ""

    def __init__(self) -> None:
        if not self.name:
            self.name = type(self).__name__.lower().replace("detector", "")

    @abstractmethod
    def detect_sync(self, text: str, context: Context) -> Detection: ...

    async def detect(self, text: str, context: Context) -> Detection:
        return self.detect_sync(text, context)
