"""The vault kind: where a session's mapping is kept.

The privacy engine depends only on :class:`Vault`, so a backend can be swapped
without touching the engine.  A backend that keeps records as bytes extends
:class:`StoredVault`, which writes the record format once for all of them.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import final

from privyx.core import Component, Session, VaultError


class Vault(Component, kind="vault"):
    """Where sessions are kept: create, read, save, and delete one by id."""

    @abstractmethod
    async def create(self, session: Session) -> None:
        """Store a new session.

        Raises:
            VaultError: If a live session has its id.
        """

    @abstractmethod
    async def get(self, session_id: str) -> Session | None:
        """The session, or ``None`` if there is none or it expired."""

    @abstractmethod
    async def save(self, session: Session, *, lock: str | None = None) -> None:
        """Store ``session`` over the one stored; with ``lock``, only while :meth:`lock` holds it.

        Raises:
            VaultError: If ``lock`` no longer holds the session.
        """

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove the session, if there is one."""

    async def list_sessions(self) -> list[Session]:
        """Every live (unexpired) session, for ``privyx session list/prune``.

        Optional: the default raises :class:`VaultError`, so a vault that
        cannot list still serves the proxy.
        """
        raise VaultError(f"the {self.name} vault cannot list sessions")

    def lock(self, session_id: str) -> AbstractAsyncContextManager[str] | None:
        """Hold ``session_id`` across processes; :meth:`save` takes the token it yields.

        ``None``, the default, when the vault is this process's alone: the
        engine's own lock is enough then.
        """
        return None


class StoredVault(Vault):
    """A vault that keeps each session as a record of bytes, by session id.

    A backend stores, reads, and lists records; the session methods, final
    here, write the record format (the session as JSON) once for every
    backend.
    """

    @abstractmethod
    async def read(self, key: str) -> bytes | None:
        """The record stored under ``key``, or ``None`` if there is none or it expired."""

    @abstractmethod
    async def write(
        self,
        key: str,
        value: bytes,
        *,
        updated_at: float,
        new: bool = False,
        lock: str | None = None,
    ) -> bool:
        """Store ``value`` under ``key``; whether it was stored.

        Args:
            key: The session id.
            value: The record.
            updated_at: When the session was last used, a Unix time; a TTL
                may count from it.
            new: Store it only if no live record has ``key``.
            lock: Store it only while :meth:`lock` holds ``key`` with this token.
        """

    @abstractmethod
    async def remove(self, key: str) -> None:
        """Remove the record under ``key``, if there is one."""

    @abstractmethod
    def keys(self) -> AsyncIterator[str]:
        """The key of each live record."""

    @final
    async def create(self, session: Session) -> None:
        if not await self.write(
            session.session_id, _encode(session), updated_at=session.updated_at, new=True
        ):
            raise VaultError(f"session already exists: {session.session_id}")

    @final
    async def get(self, session_id: str) -> Session | None:
        record = await self.read(session_id)
        if record is None:
            return None
        try:
            return Session.from_dict(json.loads(record))
        except (json.JSONDecodeError, KeyError) as exc:
            raise VaultError(f"corrupt session record: {session_id}") from exc

    @final
    async def save(self, session: Session, *, lock: str | None = None) -> None:
        if not await self.write(
            session.session_id, _encode(session), updated_at=session.updated_at, lock=lock
        ):
            raise VaultError(f"session lock expired before saving: {session.session_id}")

    @final
    async def delete(self, session_id: str) -> None:
        await self.remove(session_id)

    @final
    async def list_sessions(self) -> list[Session]:
        sessions = []
        async for key in self.keys():
            session = await self.get(key)
            if session is not None:  # it expired since it was listed
                sessions.append(session)
        return sessions


def _encode(session: Session) -> bytes:
    """The record of ``session``."""
    return json.dumps(session.to_dict()).encode()
