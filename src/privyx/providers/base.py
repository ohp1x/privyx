"""Provider protocol and base class.

Providers are transports: they know how to talk to an upstream API, but have
no knowledge of privacy logic.  The generic provider is the default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    """Protocol for provider transports.

    ``stream`` is declared ``def`` returning an ``AsyncGenerator``, not
    ``async def``: an async generator function is called to *get* the iterator,
    not awaited.  Declaring it ``async def`` here would type the result as a
    coroutine and make ``async for provider.stream(...)`` a type error.
    """

    async def send(self, payload: Any, session_id: str | None = None) -> Any: ...

    def stream(self, payload: Any, session_id: str | None = None) -> AsyncGenerator[Any, None]: ...

    async def close(self) -> None: ...


class BaseProvider(ABC):
    """Convenience base class for providers."""

    name: str = "generic"

    @abstractmethod
    async def send(self, payload: Any, session_id: str | None = None) -> Any: ...

    @abstractmethod
    def stream(self, payload: Any, session_id: str | None = None) -> AsyncGenerator[Any, None]: ...

    async def close(self) -> None:
        """Release transport resources.  Override if the provider owns any."""
        return None
