"""In-memory vault (single process, not for production)."""

from __future__ import annotations

import asyncio

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault


class MemoryVault(BaseVault):
    """Thread-safe in-memory vault.

    Suitable for development and single-process deployments.  Do not use in
    multi-instance production — state is lost on restart.
    """

    name = "memory"

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, session: Session) -> None:
        async with self._lock:
            if session.session_id in self._sessions:
                raise VaultError(f"session already exists: {session.session_id}")
            self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)