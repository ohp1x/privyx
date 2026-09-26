"""Tests for vault backends."""

from __future__ import annotations

import pytest

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.memory import MemoryVault


@pytest.mark.asyncio
async def test_memory_vault_create_get_save_delete() -> None:
    vault = MemoryVault()
    session = Session()
    await vault.create(session)
    assert (await vault.get(session.session_id)) is not None

    session.put("<PRIVYX_EMAIL_1>", "alice@example.com")
    await vault.save(session)

    loaded = await vault.get(session.session_id)
    assert loaded is not None
    assert loaded.get("<PRIVYX_EMAIL_1>") == "alice@example.com"

    await vault.delete(session.session_id)
    assert (await vault.get(session.session_id)) is None


@pytest.mark.asyncio
async def test_memory_vault_duplicate_create_raises() -> None:
    vault = MemoryVault()
    session = Session()
    await vault.create(session)
    with pytest.raises(VaultError):
        await vault.create(session)


@pytest.mark.asyncio
async def test_session_roundtrip_dict() -> None:
    session = Session()
    session.put("<PRIVYX_EMAIL_1>", "bob@example.com")
    restored = Session.from_dict(session.to_dict())
    assert restored.get("<PRIVYX_EMAIL_1>") == "bob@example.com"
    assert restored.pseudonym_for("bob@example.com") == "<PRIVYX_EMAIL_1>"
