"""SQLite-backed vault for single-server persistence.

Requires the optional ``sqlite`` extra (aiosqlite).  Uses a simple
key/value table for session records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SQLiteVault(BaseVault):
    """Vault backed by a SQLite database.

    Args:
        dsn: Path to the SQLite file (``":memory:"`` supported).
    """

    name = "sqlite"

    def __init__(self, dsn: str = "privyx.db") -> None:
        self._dsn = dsn
        self._db: Any = None

    async def connect(self) -> None:
        """Open the database (must be called before use)."""
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover - optional dep
            msg = "aiosqlite is required. Install with `pip install privyx[sqlite]`."
            raise VaultError(msg) from exc

        if self._dsn != ":memory:":
            Path(self._dsn).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._dsn)
        await self._db.execute(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create(self, session: Session) -> None:
        existing = await self.get(session.session_id)
        if existing is not None:
            raise VaultError(f"session already exists: {session.session_id}")
        await self._write(session)

    async def _write(self, session: Session) -> None:
        assert self._db is not None, "SQLiteVault not connected"
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions (session_id, payload, updated_at) VALUES (?, ?, ?)",
            (session.session_id, json.dumps(session.to_dict()), session.updated_at),
        )
        await self._db.commit()

    async def get(self, session_id: str) -> Session | None:
        assert self._db is not None, "SQLiteVault not connected"
        cursor = await self._db.execute(
            "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        try:
            return Session.from_dict(json.loads(row[0]))
        except (json.JSONDecodeError, KeyError) as exc:
            raise VaultError(f"corrupt session record: {session_id}") from exc

    async def save(self, session: Session) -> None:
        await self._write(session)

    async def delete(self, session_id: str) -> None:
        assert self._db is not None, "SQLiteVault not connected"
        await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await self._db.commit()
