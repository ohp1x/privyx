"""Redis-backed vault for multi-instance / production deployments.

Requires the optional ``redis`` extra.
"""

from __future__ import annotations

import json
from typing import Any

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault

_KEY_PREFIX = "privyx:session:"


class RedisVault(BaseVault):
    """Vault backed by Redis.

    Args:
        client: An async Redis client (``redis.asyncio.Redis``).
        ttl: Optional expiry for session keys (seconds).
    """

    name = "redis"

    def __init__(self, client: Any, ttl: int | None = None) -> None:
        self._client = client
        self._ttl = ttl

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def create(self, session: Session) -> None:
        existing = await self.get(session.session_id)
        if existing is not None:
            raise VaultError(f"session already exists: {session.session_id}")
        await self._write(session)

    async def _write(self, session: Session) -> None:
        data = json.dumps(session.to_dict())
        if self._ttl is not None:
            await self._client.set(self._key(session.session_id), data, ex=self._ttl)
        else:
            await self._client.set(self._key(session.session_id), data)

    async def get(self, session_id: str) -> Session | None:
        raw = await self._client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return Session.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError) as exc:
            raise VaultError(f"corrupt session record: {session_id}") from exc

    async def save(self, session: Session) -> None:
        await self._write(session)

    async def delete(self, session_id: str) -> None:
        await self._client.delete(self._key(session_id))
