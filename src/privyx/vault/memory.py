"""In-memory vault (single process, not for production)."""

from __future__ import annotations

import asyncio
import time

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault


class MemoryVault(BaseVault):
    """Thread-safe in-memory vault.

    Suitable for development and single-process deployments.  Do not use in
    multi-instance production — state is lost on restart.

    Args:
        ttl: Optional idle expiry (seconds).  A session whose ``updated_at`` is
            older than this is evicted on the next vault call.
    """

    name = "memory"

    def __init__(self, ttl: int | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    def _evict_expired(self) -> None:
        # ponytail: O(n) sweep on every call; fine for a dev/single-process vault.
        if self._ttl is None:
            return
        cutoff = time.time() - self._ttl
        self._sessions = {k: s for k, s in self._sessions.items() if s.updated_at >= cutoff}

    async def create(self, session: Session) -> None:
        async with self._lock:
            self._evict_expired()
            if session.session_id in self._sessions:
                raise VaultError(f"session already exists: {session.session_id}")
            self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            self._evict_expired()
            return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def list_sessions(self) -> list[Session]:
        async with self._lock:
            self._evict_expired()
            return list(self._sessions.values())
