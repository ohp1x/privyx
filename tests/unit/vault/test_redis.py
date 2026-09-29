"""Redis vault: atomic create, failures, and the client it builds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis.exceptions

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.redis import RedisVault, redis_client


class _Redis:
    """The commands RedisVault sends, each after a network round trip, or failing."""

    def __init__(self, error: Exception | None = None) -> None:
        self.data: dict[str, str] = {}
        self.error = error

    async def _round_trip(self) -> None:
        await asyncio.sleep(0)  # lets concurrent callers interleave, as over a socket
        if self.error is not None:
            raise self.error

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> Any:
        await self._round_trip()
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        await self._round_trip()
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        await self._round_trip()
        self.data.pop(key, None)

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        await self._round_trip()
        for key in list(self.data):
            yield key


async def test_create_is_atomic() -> None:
    """A conversation's parallel first requests all create its session: one wins."""
    vault = RedisVault(_Redis())
    results = await asyncio.gather(
        *(vault.create(Session(session_id="ses_a")) for _ in range(2)), return_exceptions=True
    )

    assert [type(r) for r in results].count(VaultError) == 1


async def test_redis_errors_raise_vault_error() -> None:
    vault = RedisVault(_Redis(redis.exceptions.ConnectionError("Connection refused")))
    for call in (
        vault.create(Session()),
        vault.get("ses_x"),
        vault.save(Session()),
        vault.delete("ses_x"),
        vault.list_sessions(),
    ):
        with pytest.raises(VaultError, match="Connection refused"):
            await call


async def test_client_times_out_and_retries_only_a_dropped_connection() -> None:
    client = redis_client("redis://localhost:6379/0")
    options = client.connection_pool.connection_kwargs
    attempts = 0

    async def dropped_once() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise redis.exceptions.ConnectionError("Connection closed by server.")
        return "ok"

    async def frozen() -> str:
        nonlocal attempts
        attempts += 1
        raise redis.exceptions.TimeoutError("Timeout reading from socket")

    async def reconnect(error: Exception) -> None:
        return None

    try:
        assert options["socket_timeout"] == options["socket_connect_timeout"] == 5
        assert await options["retry"].call_with_retry(dropped_once, reconnect) == "ok"
        attempts = 0
        with pytest.raises(redis.exceptions.TimeoutError):
            await options["retry"].call_with_retry(frozen, reconnect)
        assert attempts == 1  # a frozen Redis is not waited on twice
    finally:
        await client.aclose()

    tuned = redis_client("redis://localhost:6379/0?socket_timeout=2")
    assert tuned.connection_pool.connection_kwargs["socket_timeout"] == 2
    await tuned.aclose()
