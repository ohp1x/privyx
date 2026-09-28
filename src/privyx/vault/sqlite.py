"""SQLite-backed vault for single-server persistence.

Requires the optional ``sqlite`` extra (aiosqlite).  Uses a simple
key/value table for session records.  ``updated_at`` mirrors the session's
last activity and drives the optional TTL.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault

#: A commit appends to the write-ahead log instead of syncing the database and
#: a rollback journal: ~4.5 ms of disk I/O per request instead of ~11.  A power
#: loss can undo the last commits but not corrupt the file.  WAL needs a local
#: disk; it does not work on a network file system such as NFS.  A lock held by
#: another process is waited on for sqlite3's default 5 s busy timeout.
_PRAGMAS = "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_updated_at ON sessions (updated_at);
"""

#: Seconds between two sweeps of expired rows, which reads skip meanwhile.
_SWEEP_INTERVAL = 60.0


class SQLiteVault(BaseVault):
    """Vault backed by a SQLite database.

    Args:
        dsn: Path to the SQLite file (``":memory:"`` supported).
        ttl: Optional idle expiry (seconds).  Expired rows are invisible to
            reads and deleted when a new session is created, at most once a
            minute.

    A failed database call raises :class:`VaultError`.
    """

    name = "sqlite"

    def __init__(self, dsn: str = "privyx.db", ttl: int | None = None) -> None:
        self._dsn = dsn
        self._ttl = ttl
        self._db: Any = None
        self._next_sweep = 0.0  # time.monotonic() of the next TTL sweep

    def _cutoff(self) -> float:
        """Oldest live ``updated_at``; 0 (every epoch timestamp) without a TTL."""
        return 0.0 if self._ttl is None else time.time() - self._ttl

    async def connect(self) -> None:
        """Open the database (must be called before use)."""
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover - optional dep
            msg = "aiosqlite is required. Install with `pip install privyx[sqlite]`."
            raise VaultError(msg) from exc

        if self._dsn != ":memory:":
            Path(self._dsn).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = await aiosqlite.connect(self._dsn)
            await self._db.executescript(_PRAGMAS + _SCHEMA)
            await self._db.commit()
        except sqlite3.Error as exc:
            # An open connection's thread would keep the process from exiting.
            await self.close()
            raise VaultError(f"cannot open the sqlite vault {self._dsn}: {exc}") from exc

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _execute(
        self, sql: str, params: tuple[Any, ...] = (), *, commit: bool = False
    ) -> list[Any]:
        """Run one statement and return its rows; a failure raises VaultError."""
        assert self._db is not None, "SQLiteVault not connected"
        try:
            rows = list(await self._db.execute_fetchall(sql, params))
            if commit:
                await self._db.commit()
        except sqlite3.Error as exc:
            # A write left open would hold the lock against other processes
            # (`privyx session prune`) until this connection's next commit.
            with contextlib.suppress(sqlite3.Error):
                await self._db.rollback()
            raise VaultError(f"sqlite vault: {exc}") from exc
        return rows

    async def create(self, session: Session) -> None:
        if self._ttl is not None and time.monotonic() >= self._next_sweep:
            # Without this sweep, sticky sessions nobody asks for again would
            # sit on disk forever.
            self._next_sweep = time.monotonic() + _SWEEP_INTERVAL
            await self._execute(
                "DELETE FROM sessions WHERE updated_at < ?", (self._cutoff(),), commit=True
            )
        existing = await self.get(session.session_id)
        if existing is not None:
            raise VaultError(f"session already exists: {session.session_id}")
        await self._write(session)

    async def _write(self, session: Session) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO sessions (session_id, payload, updated_at) VALUES (?, ?, ?)",
            (session.session_id, json.dumps(session.to_dict()), session.updated_at),
            commit=True,
        )

    async def get(self, session_id: str) -> Session | None:
        rows = await self._execute(
            "SELECT payload FROM sessions WHERE session_id = ? AND updated_at >= ?",
            (session_id, self._cutoff()),
        )
        if not rows:
            return None
        try:
            return Session.from_dict(json.loads(rows[0][0]))
        except (json.JSONDecodeError, KeyError) as exc:
            raise VaultError(f"corrupt session record: {session_id}") from exc

    async def save(self, session: Session) -> None:
        await self._write(session)

    async def delete(self, session_id: str) -> None:
        await self._execute("DELETE FROM sessions WHERE session_id = ?", (session_id,), commit=True)

    async def list_sessions(self) -> list[Session]:
        rows = await self._execute(
            "SELECT session_id, payload FROM sessions WHERE updated_at >= ?", (self._cutoff(),)
        )
        sessions = []
        for session_id, payload in rows:
            try:
                sessions.append(Session.from_dict(json.loads(payload)))
            except (json.JSONDecodeError, KeyError) as exc:
                raise VaultError(f"corrupt session record: {session_id}") from exc
        return sessions
