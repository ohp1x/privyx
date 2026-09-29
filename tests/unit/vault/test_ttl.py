"""Vault TTL and session listing across the built-in backends."""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from privyx.core.session import Session
from privyx.vault.base import BaseVault
from privyx.vault.memory import MemoryVault
from privyx.vault.redis import RedisVault
from privyx.vault.sqlite import SQLiteVault

MakeVault = Callable[..., Awaitable[BaseVault]]


@pytest.fixture(params=["memory", "sqlite"])
async def make_vault(request: pytest.FixtureRequest) -> AsyncIterator[MakeVault]:
    opened: list[SQLiteVault] = []

    async def make(ttl: int | None = None) -> BaseVault:
        if request.param == "memory":
            return MemoryVault(ttl=ttl)
        vault = SQLiteVault(":memory:", ttl=ttl)
        await vault.connect()
        opened.append(vault)
        return vault

    yield make
    for vault in opened:
        await vault.close()


async def _idle(vault: BaseVault, seconds: float) -> Session:
    session = Session()
    await vault.create(session)
    session.updated_at -= seconds
    await vault.save(session)
    return session


async def test_ttl_hides_idle_sessions(make_vault: MakeVault) -> None:
    vault = await make_vault(ttl=60)
    idle = await _idle(vault, 120)
    live = await _idle(vault, 10)

    assert await vault.get(idle.session_id) is None
    assert [s.session_id for s in await vault.list_sessions()] == [live.session_id]
    # An expired id is free again: the proxy re-creates a sticky session under it.
    await vault.create(Session(session_id=idle.session_id))


async def test_without_ttl_sessions_never_expire(make_vault: MakeVault) -> None:
    vault = await make_vault()
    old = await _idle(vault, 10 * 365 * 86400)

    assert await vault.get(old.session_id) is not None
    assert len(await vault.list_sessions()) == 1


async def test_sqlite_deletes_expired_rows_from_disk() -> None:
    """Hidden is not enough: an expired mapping must not stay in the file."""
    vault = SQLiteVault(":memory:", ttl=60)
    await vault.connect()
    try:
        await _idle(vault, 120)  # its create ran this minute's sweep
        await vault.create(Session())  # no second sweep within the minute
        assert await vault._execute("SELECT count(*) FROM sessions") == [(2,)]

        vault._next_sweep -= 60  # a minute later, a new session sweeps the idle one
        await vault.create(Session())
        assert await vault._execute("SELECT count(*) FROM sessions") == [(2,)]
    finally:
        await vault.close()


class _FakeRedis:
    """The slice of ``redis.asyncio.Redis`` RedisVault uses; keys come back as bytes."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> Any:
        if nx and key in self.data:
            return None
        self.data[key] = value.encode()
        return True

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def scan_iter(self, match: str) -> AsyncIterator[bytes]:
        for key in list(self.data):
            if fnmatch.fnmatch(key, match):
                yield key.encode()


async def test_redis_lists_only_its_own_keys() -> None:
    client: Any = _FakeRedis()
    vault = RedisVault(client)
    session = Session()
    session.put("<PRIVYX_EMAIL_1>", "bob@example.com")
    await vault.create(session)
    client.data["other-app:key"] = b"{}"

    listed = await vault.list_sessions()

    assert [s.session_id for s in listed] == [session.session_id]
    assert listed[0].get("<PRIVYX_EMAIL_1>") == "bob@example.com"
