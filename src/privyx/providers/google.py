"""Google Gemini provider adapter.

Gemini streaming uses a different envelope (``generateContent`` style SSE).
This is a stub to keep the module layout complete; the generic SSE adapter
covers most cases.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from privyx.providers.base import BaseProvider


class GoogleProvider(BaseProvider):
    """Google Gemini provider (placeholder)."""

    name = "google"

    async def send(self, payload: Any, session_id: str | None = None) -> Any:
        raise NotImplementedError("GoogleProvider is not yet implemented")

    async def stream(
        self, payload: Any, session_id: str | None = None
    ) -> AsyncGenerator[Any, None]:
        raise NotImplementedError("GoogleProvider is not yet implemented")
        # satisfy the async generator protocol
        yield  # pragma: no cover
