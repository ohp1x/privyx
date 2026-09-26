"""Anchor protocol and base class.

Anchors make pseudonyms verifiable or key-bound without storing a mapping in
the vault.  The classic example is HMAC-based anchoring: ``HMAC(key, value)``
derives a deterministic pseudonym that can be re-derived for deanonymization
(no lookup table needed, at the cost of one-way-verifiability rather than
stateful reversibility).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.context import Context


@runtime_checkable
class Anchor(Protocol):
    """Protocol for anchors."""

    async def anchor(self, text: str, context: Context) -> str: ...

    async def deanchor(self, anchored: str, context: Context) -> str | None: ...


class BaseAnchor(ABC):
    """Convenience base class for anchors."""

    name: str = ""

    @abstractmethod
    async def anchor(self, text: str, context: Context) -> str: ...

    @abstractmethod
    async def deanchor(self, anchored: str, context: Context) -> str | None: ...
