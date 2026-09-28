"""Streaming HTTP proxy utilities for the proxy layer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


class AuditedStream[T]:
    """Wrap an async generator to ensure cleanup and upstream closure on aclose.

    Python async generators do not execute their try/finally blocks if closed
    before the first item is pulled (in the GEN_CREATED state).  Wrapping the
    stream guarantees that upstream response resources and ephemeral sessions
    are released even when the client disconnects before consuming any chunks.
    """

    def __init__(
        self,
        stream: AsyncIterator[T],
        *,
        response: Any | None = None,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._stream = stream
        self._response = response
        self._cleanup = cleanup
        self._cleaned = False

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await anext(self._stream)

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        finally:
            await self.close_cleanup()

    async def close_cleanup(self) -> None:
        if not self._cleaned:
            self._cleaned = True
            try:
                if self._response is not None and hasattr(self._response, "aclose"):
                    await self._response.aclose()
            finally:
                if self._cleanup is not None:
                    await self._cleanup()
