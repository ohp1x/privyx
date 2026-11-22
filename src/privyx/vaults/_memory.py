"""In-memory vault (single process, not for production)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import Field

from privyx.core import Session, VaultError
from privyx.vaults._base import Vault


class MemoryVault(Vault):
    """Thread-safe in-memory vault.

    Suitable for development and single-process deployments.  Do not use in
    multi-instance production — state is lost on restart.  Sessions stay
    objects in memory, not records.
    """

    name = "memory"

    class Options(Vault.Options):
        # Idle expiry in seconds, measured from a session's last transform.  Unset
        # keeps sessions until deleted; 0 is rejected (it would expire every
        # session on creation, and Redis refuses it).
        ttl: int | None = Field(default=None, gt=0)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def _evict_expired(self) -> None:
        # An O(n) sweep on every call: fine for a dev or single-process vault.
        if self.options.ttl is None:
            return
        cutoff = time.time() - self.options.ttl
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

    async def save(self, session: Session, *, lock: str | None = None) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def list_sessions(self) -> list[Session]:
        async with self._lock:
            self._evict_expired()
            return list(self._sessions.values())
