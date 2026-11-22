"""SQLite-backed vault for single-server persistence.

Requires the optional ``sqlite`` extra (aiosqlite).  Uses a simple
key/value table for session records.  ``updated_at`` mirrors the session's
last activity and drives the optional TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import Field

from privyx.core import ConfigError, Services, VaultError
from privyx.vaults._base import StoredVault

#: A commit appends to the write-ahead log instead of syncing the database and
#: a rollback journal: ~4.5 ms of disk I/O per request instead of ~11.  A power
#: loss can undo the last commits but not corrupt the file.  WAL needs a local
#: disk; it does not work on a network file system such as NFS.  A lock held by
#: another process is waited on for sqlite3's default 5 s busy timeout.
#: Reads go through memory-mapped I/O (up to 256 MB), the page cache may grow
#: to 64 MB (a cap, not allocated up front), and temporary tables stay in RAM.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL; "
    "PRAGMA synchronous=NORMAL; "
    "PRAGMA mmap_size=268435456; "
    "PRAGMA cache_size=-64000; "
    "PRAGMA temp_store=MEMORY;"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_updated_at ON sessions (updated_at);
CREATE TABLE IF NOT EXISTS session_locks (
    session_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""

#: Seconds between two sweeps of expired rows, which reads skip meanwhile.
_SWEEP_INTERVAL = 60.0


class SQLiteVault(StoredVault):
    """Vault backed by a SQLite database, opened by :meth:`open`.

    Expired rows are invisible to reads and deleted when a new session is
    created, at most once a minute.  A failed database call raises
    :class:`VaultError`.
    """

    name = "sqlite"

    class Options(StoredVault.Options):
        # Idle expiry in seconds, measured from a session's last transform.  Unset
        # keeps sessions until deleted; 0 is rejected (it would expire every
        # session on creation, and Redis refuses it).
        ttl: int | None = Field(default=None, gt=0)
        #: A path, ``~`` for the home directory, or a SQLAlchemy-style URL
        #: (``sqlite+aiosqlite:///``); ``:memory:`` for a database that goes
        #: with the vault.
        dsn: str = "sqlite+aiosqlite:///~/.privyx/privyx.db"

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._dsn = sqlite_path(self.options.dsn)
        self.persistent = self._dsn != ":memory:"
        self._ttl = self.options.ttl
        self._db: Any = None
        self._next_sweep = 0.0  # time.monotonic() of the next TTL sweep

    def _cutoff(self) -> float:
        """Oldest live ``updated_at``; 0 (every epoch timestamp) without a TTL."""
        return 0.0 if self._ttl is None else time.time() - self._ttl

    async def open(self, services: Services) -> None:
        """Open the database, creating its file and tables when they are missing."""
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover - optional dep
            msg = "aiosqlite is required. Install with `pip install privyx[sqlite]`."
            raise VaultError(msg) from exc

        if self._dsn != ":memory:":
            Path(self._dsn).parent.mkdir(parents=True, exist_ok=True)
            # Sessions hold original values, so a new file is owner-only; SQLite
            # creates the -wal and -shm files with the database file's mode.
            with contextlib.suppress(OSError):  # it exists, or connect() says why not
                Path(self._dsn).touch(mode=0o600, exist_ok=False)  # noqa: ASYNC240 - once per open
            if os.name == "posix":
                with contextlib.suppress(OSError):
                    os.chmod(self._dsn, 0o600)
                for ext in ("-wal", "-shm"):
                    with contextlib.suppress(OSError):
                        os.chmod(f"{self._dsn}{ext}", 0o600)
        try:
            self._db = await aiosqlite.connect(self._dsn)
            # Switching a new file to WAL and creating its tables take locks
            # SQLite does not wait for, so processes opening it at once retry
            # the (idempotent) setup instead of failing.
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    await self._db.executescript(_PRAGMAS + _SCHEMA)
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.05)
            await self._db.commit()
            if os.name == "posix" and self._dsn != ":memory:":
                for ext in ("-wal", "-shm"):
                    with contextlib.suppress(OSError):
                        os.chmod(f"{self._dsn}{ext}", 0o600)
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

    async def write(
        self,
        key: str,
        value: bytes,
        *,
        updated_at: float,
        new: bool = False,
        lock: str | None = None,
    ) -> bool:
        payload = value.decode()
        if new:
            if self._ttl is not None and time.monotonic() >= self._next_sweep:
                # Without this sweep, sticky sessions nobody asks for again would
                # sit on disk forever.
                self._next_sweep = time.monotonic() + _SWEEP_INTERVAL
                await self._execute(
                    "DELETE FROM sessions WHERE updated_at < ?", (self._cutoff(),), commit=True
                )
            # One statement, so parallel creates of one id (a conversation's first
            # requests) cannot all succeed; an expired row may be taken over.
            return bool(
                await self._execute(
                    "INSERT INTO sessions (session_id, payload, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at "
                    "WHERE sessions.updated_at < ? RETURNING session_id",
                    (key, payload, updated_at, self._cutoff()),
                    commit=True,
                )
            )
        if lock is None:
            await self._execute(
                "INSERT OR REPLACE INTO sessions (session_id, payload, updated_at) "
                "VALUES (?, ?, ?)",
                (key, payload, updated_at),
                commit=True,
            )
            return True
        return bool(
            await self._execute(
                "INSERT OR REPLACE INTO sessions (session_id, payload, updated_at) "
                "SELECT ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM session_locks WHERE session_id = ? AND token = ?) "
                "RETURNING session_id",
                (key, payload, updated_at, key, lock),
                commit=True,
            )
        )

    async def read(self, key: str) -> bytes | None:
        rows = await self._execute(
            "SELECT payload FROM sessions WHERE session_id = ? AND updated_at >= ?",
            (key, self._cutoff()),
        )
        return str(rows[0][0]).encode() if rows else None

    @asynccontextmanager
    async def lock(self, session_id: str, ttl: int = 10, wait: float = 10.0) -> AsyncIterator[str]:
        """Hold ``session_id`` across processes sharing this database file.

        Yields the token :meth:`save` checks.  The lock is renewed every
        ``ttl / 3`` seconds while held, so a slow transform keeps it; a process
        that dies leaves a row another may take over after ``ttl``.  Waiting
        longer than ``wait`` seconds for it raises :class:`VaultError`.
        """
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait
        while not await self._execute(
            "INSERT INTO session_locks (session_id, token, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "token = excluded.token, expires_at = excluded.expires_at "
            "WHERE session_locks.expires_at < ? RETURNING token",
            (session_id, token, time.time() + ttl, time.time()),
            commit=True,
        ):
            if time.monotonic() >= deadline:
                raise VaultError(f"timed out waiting for the session lock: {session_id}")
            await asyncio.sleep(0.05)

        async def renew() -> None:
            while True:
                await asyncio.sleep(ttl / 3)
                # A missed renewal is not fatal: save still checks the token.
                with contextlib.suppress(VaultError):
                    await self._execute(
                        "UPDATE session_locks SET expires_at = ? "
                        "WHERE session_id = ? AND token = ?",
                        (time.time() + ttl, session_id, token),
                        commit=True,
                    )

        renewer = asyncio.create_task(renew())
        try:
            yield token
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            with contextlib.suppress(VaultError):  # unreleased, it is taken over after ttl
                await self._execute(
                    "DELETE FROM session_locks WHERE session_id = ? AND token = ?",
                    (session_id, token),
                    commit=True,
                )

    async def remove(self, key: str) -> None:
        await self._execute("DELETE FROM sessions WHERE session_id = ?", (key,), commit=True)

    async def keys(self) -> AsyncIterator[str]:
        rows = await self._execute(
            "SELECT session_id FROM sessions WHERE updated_at >= ?", (self._cutoff(),)
        )
        for (session_id,) in rows:
            yield session_id


def sqlite_path(dsn: str) -> str:
    """The file of ``dsn``: a bare path or a SQLAlchemy-style URL; ``~`` is the home directory.

    ``:memory:`` for a database in this process's memory: ``:memory:``, or an
    empty path.

    Raises:
        ConfigError: If ``dsn`` is a URL of another form, such as ``sqlite://:memory:``.
    """
    path = dsn
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]+:", path):  # a URL scheme; a drive (C:) is a path
        raise ConfigError(
            f"vault.dsn {dsn!r} is not a form the sqlite vault reads; "
            "write a path, sqlite+aiosqlite:///PATH, or :memory:"
        )
    return os.path.expanduser(path) or ":memory:"
