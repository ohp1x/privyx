"""SQLite vault: journal mode, atomic create, file modes, and database errors."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.vault.sqlite import SQLiteVault


async def test_writes_through_a_wal_journal(tmp_path: Path) -> None:
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    try:
        assert await vault._execute("PRAGMA journal_mode") == [("wal",)]
        assert await vault._execute("PRAGMA synchronous") == [(1,)]  # NORMAL
        assert await vault._execute("PRAGMA mmap_size") == [(268435456,)]
        assert await vault._execute("PRAGMA cache_size") == [(-64000,)]
        assert await vault._execute("PRAGMA temp_store") == [(2,)]  # MEMORY
    finally:
        await vault.close()


async def test_parallel_first_requests_create_one_session(tmp_path: Path) -> None:
    """A conversation's parallel first requests share its sticky id: one creates it."""
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    audit = io.StringIO()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=AuditLogger(audit),
    )
    try:
        sessions = await asyncio.gather(
            *(engine.get_or_create_session("ses_a", source="conversation") for _ in range(50))
        )
    finally:
        await vault.close()

    assert {s.session_id for s in sessions} == {"ses_a"}
    assert audit.getvalue().count('"session.created"') == 1


async def test_create_does_not_overwrite_a_live_session() -> None:
    vault = SQLiteVault(":memory:", ttl=60)
    await vault.connect()
    try:
        saved = Session(session_id="ses_a", mapping={"<EMAIL_1>": "alice@example.com"})
        await vault.create(saved)
        with pytest.raises(VaultError, match="already exists"):
            await vault.create(Session(session_id="ses_a"))
        stored = await vault.get("ses_a")
        assert stored is not None and stored.mapping == saved.mapping
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


async def test_new_files_are_owner_only(tmp_path: Path) -> None:
    """The database and its -wal and -shm files hold original values."""
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    umask = os.umask(0o022)  # SQLite's own default would then be 0644
    try:
        await vault.connect()
        await vault.create(Session(mapping={"<EMAIL_1>": "ann@example.com"}))
        modes = {
            name: (tmp_path / name).stat().st_mode & 0o777
            for name in ("privyx.db", "privyx.db-wal", "privyx.db-shm")
        }
    finally:
        os.umask(umask)
        await vault.close()

    assert modes == {"privyx.db": 0o600, "privyx.db-wal": 0o600, "privyx.db-shm": 0o600}


async def test_an_existing_file_is_tightened_to_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "privyx.db"
    path.touch()
    path.chmod(0o644)
    vault = SQLiteVault(str(path))
    await vault.connect()
    await vault.close()

    assert path.stat().st_mode & 0o777 == 0o600


async def test_a_lock_is_waited_for_and_renewed(tmp_path: Path) -> None:
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    try:
        async with vault.lock("s1", ttl=1):
            await asyncio.sleep(1.2)  # past the 1 s ttl: only renewal keeps it
            with pytest.raises(VaultError, match="timed out"):
                async with vault.lock("s1", wait=0.1):
                    pass
        async with vault.lock("s1", wait=0.1):  # released
            pass
    finally:
        await vault.close()


async def test_an_expired_lock_is_taken_over(tmp_path: Path) -> None:
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    try:
        await vault._execute(
            "INSERT INTO session_locks VALUES ('s1', 'dead', 0)", commit=True
        )  # left by a process that died
        async with vault.lock("s1", wait=0.1):
            pass
    finally:
        await vault.close()


async def test_a_save_after_the_lock_was_taken_over_is_refused(tmp_path: Path) -> None:
    vault = SQLiteVault(str(tmp_path / "privyx.db"))
    await vault.connect()
    try:
        session = Session(session_id="s1")
        session.put("<PRIVYX_EMAIL_1>", "ana@example.com")
        await vault.create(session)
        await vault._execute("INSERT INTO session_locks VALUES ('s1', 'other', 1e12)", commit=True)

        session.put("<PRIVYX_EMAIL_1>", "bob@example.com")
        with pytest.raises(VaultError, match="lock expired"):
            await vault.save(session, lock_token="mine")
        stored = await vault.get("s1")
        assert stored is not None and stored.get("<PRIVYX_EMAIL_1>") == "ana@example.com"
    finally:
        await vault.close()


async def test_opening_a_new_file_waits_for_another_process_switching_it_to_wal(
    tmp_path: Path,
) -> None:
    """Processes that start together on a new file must not fail with "locked"."""
    import sqlite3

    path = tmp_path / "privyx.db"
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("CREATE TABLE t (x)")  # a file still in rollback-journal mode
    other.execute("BEGIN IMMEDIATE")  # writing, as a process creating the schema does
    vault = SQLiteVault(str(path))
    opening = asyncio.ensure_future(vault.connect())
    await asyncio.sleep(0.3)
    other.execute("COMMIT")
    other.close()
    await opening
    try:
        assert await vault._execute("PRAGMA journal_mode") == [("wal",)]
    finally:
        await vault.close()
