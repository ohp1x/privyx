"""SQLite vault: journal mode and database errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.sqlite import SQLiteVault


async def test_writes_through_a_wal_journal(tmp_path: Path) -> None:
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    try:
        assert await vault._execute("PRAGMA journal_mode") == [("wal",)]
        assert await vault._execute("PRAGMA synchronous") == [(1,)]  # NORMAL
    finally:
        await vault.close()


async def test_database_errors_raise_vault_error() -> None:
    vault = SQLiteVault(":memory:")
    await vault.connect()
    try:
        await vault._execute("DROP TABLE sessions")
        for call in (
            vault.create(Session()),
            vault.get("ses_x"),
            vault.save(Session()),
            vault.delete("ses_x"),
            vault.list_sessions(),
        ):
            with pytest.raises(VaultError, match="no such table"):
                await call
    finally:
        await vault.close()


async def test_a_file_that_is_not_a_database_fails_to_connect(tmp_path: Path) -> None:
    path = tmp_path / "privyx.db"
    path.write_text("not a database " * 10)
    vault = SQLiteVault(str(path))
    try:
        with pytest.raises(VaultError, match="cannot open the sqlite vault"):
            await vault.connect()
        assert vault._db is None  # closed: its worker thread would keep the process alive
    finally:
        await vault.close()
