"""Vault protocol and base class.

A Vault is the persistence abstraction for session state.  The privacy engine
depends only on this interface, so backends can be swapped without touching
core logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.session import Session


@runtime_checkable
class Vault(Protocol):
    """Protocol implemented by all session vaults."""

    async def create(self, session: Session) -> None: ...

    async def get(self, session_id: str) -> Session | None: ...

    async def save(self, session: Session) -> None: ...

    async def delete(self, session_id: str) -> None: ...


class BaseVault(ABC):
    """Convenience base class for vaults."""

    name: str = "base"

    @abstractmethod
    async def create(self, session: Session) -> None: ...

    @abstractmethod
    async def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def save(self, session: Session) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...