"""Redis-backed vault for multi-instance / production deployments.

Requires the optional ``redis`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from redis.asyncio import Redis, from_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from privyx.core.errors import VaultError
from privyx.core.session import Session
from privyx.vault.base import BaseVault

_KEY_PREFIX = "privyx:session:"
_LOCK_PREFIX = "privyx:lock:"  # outside _KEY_PREFIX, so list_sessions never sees a lock

# Each checks the lock still holds this token: a holder whose lock expired must
# not renew, release, or save over the next holder's.
_SAVE_LOCKED = """
if redis.call('get', KEYS[2]) ~= ARGV[1] then return 0 end
if ARGV[3] == '' then
    redis.call('set', KEYS[1], ARGV[2])
else
    redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
end
return 1
"""
_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def redis_client(url: str) -> Redis:
    """An async client for ``url`` that fails instead of waiting on a dead Redis.

    Before redis-py 8, a call to a Redis that stopped answering waited forever;
    5 s is redis-py 8's own default, set here for every version.  A pooled
    connection that Redis closed (a restart) fails its next command, so a
    connection error is retried once, on a fresh connection.  A timeout is not:
    a frozen Redis would hold every request twice as long.  Query parameters in
    the URL (``?socket_timeout=2``) override these timeouts.
    """
    return from_url(
        url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry=Retry(NoBackoff(), 1, supported_errors=(RedisConnectionError,)),
    )


@contextmanager
def _errors() -> Iterator[None]:
    """Raise a failed Redis call as :class:`VaultError`."""
    try:
        yield
    except RedisError as exc:
        raise VaultError(f"redis vault: {exc}") from exc


class RedisVault(BaseVault):
    """Vault backed by Redis.

    Args:
        client: An async Redis client (``redis.asyncio.Redis``); see
            :func:`redis_client`.
        ttl: Optional expiry for session keys (seconds).

    A failed Redis call raises :class:`VaultError`.
    """

    name = "redis"

    def __init__(self, client: Any, ttl: int | None = None) -> None:
        self._client = client
        self._ttl = ttl

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def create(self, session: Session) -> None:
        # SET NX, not GET then SET: of two requests creating one id, one fails.
        if not await self._write(session, nx=True):
            raise VaultError(f"session already exists: {session.session_id}")

    async def _write(self, session: Session, *, nx: bool = False) -> bool:
        data = json.dumps(session.to_dict())
        with _errors():
            written = await self._client.set(
                self._key(session.session_id), data, ex=self._ttl, nx=nx
            )
        return bool(written)

    async def get(self, session_id: str) -> Session | None:
        with _errors():
            raw = await self._client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return Session.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError) as exc:
            raise VaultError(f"corrupt session record: {session_id}") from exc

    async def save(self, session: Session, *, lock_token: str | None = None) -> None:
        """Write ``session``; with ``lock_token``, only while :meth:`lock` still holds it."""
        if lock_token is None:
            await self._write(session)
            return
        sid = session.session_id
        data = json.dumps(session.to_dict())
        with _errors():
            saved = await self._client.eval(
                _SAVE_LOCKED,
                2,
                self._key(sid),
                _LOCK_PREFIX + sid,
                lock_token,
                data,
                self._ttl or "",
            )
        if not saved:
            raise VaultError(f"session lock expired before saving: {sid}")

    @asynccontextmanager
    async def lock(self, session_id: str, ttl: int = 10, wait: float = 10.0) -> AsyncIterator[str]:
        """Hold ``session_id`` across processes sharing this Redis (``SET NX EX``).

        Yields the token :meth:`save` checks.  The lock is renewed every
        ``ttl / 3`` seconds while held, so a slow transform keeps it; a process
        that dies lets it expire after ``ttl``.  Waiting longer than ``wait``
        seconds for it raises :class:`VaultError`.
        """
        key = _LOCK_PREFIX + session_id
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait
        while True:
            with _errors():
                if await self._client.set(key, token, ex=ttl, nx=True):
                    break
            if time.monotonic() >= deadline:
                raise VaultError(f"timed out waiting for the session lock: {session_id}")
            await asyncio.sleep(0.05)

        async def renew() -> None:
            while True:
                await asyncio.sleep(ttl / 3)
                # A missed renewal is not fatal: save still checks the token.
                with contextlib.suppress(RedisError):
                    await self._client.eval(_RENEW, 1, key, token, ttl)

        renewer = asyncio.create_task(renew())
        try:
            yield token
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            with contextlib.suppress(RedisError):  # unreleased, it expires after ttl
                await self._client.eval(_RELEASE, 1, key, token)

    async def delete(self, session_id: str) -> None:
        with _errors():
            await self._client.delete(self._key(session_id))

    async def list_sessions(self) -> list[Session]:
        sessions = []
        with _errors():
            async for key in self._client.scan_iter(match=f"{_KEY_PREFIX}*"):
                if isinstance(key, bytes):
                    key = key.decode()
                session = await self.get(key.removeprefix(_KEY_PREFIX))
                if session is not None:  # it expired between SCAN and GET
                    sessions.append(session)
        return sessions
