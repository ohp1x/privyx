"""The redis vault against a real Redis: atomic create, TTL, list, reconnect, lock.

Runs only when ``PRIVYX_TEST_REDIS_URL`` is set (CI starts a Redis service);
each test uses its own session ids and deletes them, so a shared Redis is safe.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.vault.redis import RedisVault, redis_client

URL = os.environ.get("PRIVYX_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="PRIVYX_TEST_REDIS_URL not set")


@pytest.fixture
async def vault() -> AsyncIterator[RedisVault]:
    client = redis_client(URL)
    yield RedisVault(client, ttl=60)
    await client.aclose()


@pytest.fixture
async def new_id(vault: RedisVault) -> AsyncIterator[Callable[[], str]]:
    """Mint session ids for one test and delete their keys afterwards."""
    ids: list[str] = []

    def make() -> str:
        ids.append(f"ses_test_{uuid.uuid4().hex}")
        return ids[-1]

    yield make
    for session_id in ids:
        await vault.delete(session_id)


async def test_parallel_creates_of_one_id_have_one_winner(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    session_id = new_id()
    results = await asyncio.gather(
        *(vault.create(Session(session_id=session_id)) for _ in range(20)),
        return_exceptions=True,
    )

    assert [type(r) for r in results].count(VaultError) == 19


async def test_a_save_renews_the_key_ttl(vault: RedisVault, new_id: Callable[[], str]) -> None:
    session = Session(session_id=new_id())
    await vault.create(session)
    await vault._client.expire(vault._key(session.session_id), 1)
    await vault.save(session)  # a save sets the TTL again

    assert 55 <= await vault._client.ttl(vault._key(session.session_id)) <= 60


async def test_list_returns_the_sessions_it_holds(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    ids = {new_id(), new_id()}
    for session_id in ids:
        await vault.create(Session(session_id=session_id))

    assert ids <= {s.session_id for s in await vault.list_sessions()}


async def test_recovers_when_redis_drops_the_connection(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    session = Session(session_id=new_id())
    await vault.create(session)
    # Kill only this client's pooled connection, as a Redis restart would.
    connection_id = await vault._client.client_id()
    admin = redis_client(URL)
    try:
        await admin.client_kill_filter(_id=connection_id)
    finally:
        await admin.aclose()

    stored = await vault.get(session.session_id)

    assert stored is not None and stored.session_id == session.session_id


def _engine(vault: RedisVault) -> PrivacyEngine:
    """An engine without an anchor: its counter tokens need the Redis lock."""
    return PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )


async def test_two_replicas_on_one_session_mint_different_tokens(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    session_id = new_id()

    async def request(engine: PrivacyEngine, text: str) -> str:
        async with engine.session_scope(session_id) as session:
            await asyncio.sleep(0.02)  # both have read the session before either saves
            return (await engine.transform(text, session=session, persist=False)).text

    first, second = await asyncio.gather(
        request(_engine(vault), "mail ana@example.com"),
        request(_engine(vault), "mail bob@example.com"),
    )

    assert first.split()[1] != second.split()[1]
    stored = await vault.get(session_id)
    assert stored is not None and len(stored.mapping) == 2


async def test_a_held_lock_is_renewed(vault: RedisVault, new_id: Callable[[], str]) -> None:
    session_id = new_id()
    async with vault.lock(session_id, ttl=1):
        await asyncio.sleep(1.2)  # past the 1 s ttl: only renewal keeps it
        assert await vault._client.get(f"privyx:lock:{session_id}") is not None
    assert await vault._client.get(f"privyx:lock:{session_id}") is None  # released


async def test_a_save_after_the_lock_expired_is_refused(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    session = Session(session_id=new_id())
    session.put("<PRIVYX_EMAIL_1>", "ana@example.com")
    await vault.create(session)
    # The lock expired and another process holds it now.
    await vault._client.set(f"privyx:lock:{session.session_id}", "other", ex=10)

    session.put("<PRIVYX_EMAIL_1>", "bob@example.com")
    with pytest.raises(VaultError, match="lock expired"):
        await vault.save(session, lock_token="mine")

    stored = await vault.get(session.session_id)
    assert stored is not None and stored.get("<PRIVYX_EMAIL_1>") == "ana@example.com"
    await vault._client.delete(f"privyx:lock:{session.session_id}")


async def test_waiting_too_long_for_the_lock_raises(
    vault: RedisVault, new_id: Callable[[], str]
) -> None:
    session_id = new_id()
    async with vault.lock(session_id):
        with pytest.raises(VaultError, match="timed out"):
            async with vault.lock(session_id, wait=0.1):
                pass
