"""SQLite vault: journal mode, atomic create, and database errors."""

from __future__ import annotations

import asyncio
import io
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
