"""Redis-backed vault for multi-instance / production deployments.

Requires the optional ``redis`` extra, imported when the vault opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

from pydantic import Field

from privyx.core import ConfigError, Services, VaultError
from privyx.vaults._base import StoredVault

if TYPE_CHECKING:
    from redis.asyncio import Redis

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
    from redis.asyncio import from_url
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError

    return from_url(
        url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry=Retry(NoBackoff(), 1, supported_errors=(RedisConnectionError,)),
    )


@contextmanager
def _errors() -> Iterator[None]:
    """Raise a failed Redis call as :class:`VaultError`."""
    from redis.exceptions import RedisError

    try:
        yield
    except RedisError as exc:
        raise VaultError(f"redis vault: {exc}") from exc


class RedisVault(StoredVault):
    """Vault backed by Redis, connected by :meth:`open`.

    A failed Redis call raises :class:`VaultError`.

    Args:
        client: An async Redis client (``redis.asyncio.Redis``) to use instead
            of the one :meth:`open` connects to ``redis_url``; the caller
            closes it.
        **options: The vault's :class:`Options`.
    """

    name = "redis"

    class Options(StoredVault.Options):
        # Expiry of a session key in seconds, renewed by each write.  Unset keeps
        # sessions until deleted; 0 is rejected (Redis refuses it).
        ttl: int | None = Field(default=None, gt=0)
        redis_url: str = "redis://localhost:6379/0"

    options: Options

    def __init__(self, *, client: Any = None, **options: Any) -> None:
        super().__init__(**options)
        self._client = client
        self._owns_client = False

    async def open(self, services: Services) -> None:
        """Connect to ``redis_url``, unless a client was given."""
        if self._client is not None:
            return
        try:
            self._client = redis_client(self.options.redis_url)
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigError(
                "redis vault requires the redis extra: pip install privyx[redis]"
            ) from exc
        self._owns_client = True

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            self._client, self._owns_client = None, False

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def write(
        self,
        key: str,
        value: bytes,
        *,
        updated_at: float,
        new: bool = False,
        lock: str | None = None,
    ) -> bool:
        ttl = self.options.ttl
        with _errors():
            if lock is None:
                # SET NX, not GET then SET: of two requests creating one id, one fails.
                return bool(await self._client.set(self._key(key), value, ex=ttl, nx=new))
            return bool(
                await self._client.eval(
                    _SAVE_LOCKED, 2, self._key(key), _LOCK_PREFIX + key, lock, value, ttl or ""
                )
            )

    async def read(self, key: str) -> bytes | None:
        with _errors():
            raw: bytes | None = await self._client.get(self._key(key))
        return raw

    @asynccontextmanager
    async def lock(self, session_id: str, ttl: int = 10, wait: float = 10.0) -> AsyncIterator[str]:
        """Hold ``session_id`` across processes sharing this Redis (``SET NX EX``).

        Yields the token :meth:`save` checks.  The lock is renewed every
        ``ttl / 3`` seconds while held, so a slow transform keeps it; a process
        that dies lets it expire after ``ttl``.  Waiting longer than ``wait``
        seconds for it raises :class:`VaultError`.
        """
        from redis.exceptions import RedisError

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

    async def remove(self, key: str) -> None:
        with _errors():
            await self._client.delete(self._key(key))

    async def keys(self) -> AsyncIterator[str]:
        with _errors():
            async for key in self._client.scan_iter(match=f"{_KEY_PREFIX}*"):
                if isinstance(key, bytes):
                    key = key.decode()
                yield key.removeprefix(_KEY_PREFIX)
