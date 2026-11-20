"""More concurrent streams than httpx's default pool all reach the upstream.

httpx caps a client at 100 connections unless told otherwise: stream 101 onward
waited for a free one and, after 10 s, failed with a 500 (transparent) or a 502
(gateway).  The upstream here holds every response until all the streams are
open at once, under a real server, since a mock transport has no pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import uvicorn

from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.operators._pseudonym import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.providers import GenericProvider
from privyx.proxy.transparent import TransparentProxy
from privyx.vaults import MemoryVault

STREAMS = 150
BODY = {"stream": True, "messages": [{"role": "user", "content": "mail alice@example.com"}]}
FRAME = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class Upstream:
    """Answers each request once ``STREAMS`` of them are open, or after 5 s."""

    def __init__(self) -> None:
        self.open = self.peak = 0
        self.all_open = asyncio.Event()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.open += 1
        self.peak = max(self.peak, self.open)
        if self.open == STREAMS:
            self.all_open.set()
        with contextlib.suppress(TimeoutError):  # a capped pool never gets there
            await asyncio.wait_for(self.all_open.wait(), 5)
        self.open -= 1
        headers = [(b"content-type", b"text/event-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": FRAME})


def _transparent(origin: str) -> tuple[Callable[[], Awaitable[None]], Any]:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    proxy = TransparentProxy(engine, origin=origin)

    async def stream() -> None:
        result = await proxy.handle(
            method="POST",
            path="v1/messages",
            headers={"content-type": "application/json"},
            body=json.dumps(BODY).encode(),
        )
        assert result.status_code == 200
        assert result.stream is not None
        async for _ in result.stream:
            pass

    return stream, proxy


def _gateway(origin: str) -> tuple[Callable[[], Awaitable[None]], Any]:
    """What the gateway sends a stream with: the upstream client, aimed by the provider."""
    from types import SimpleNamespace

    from privyx.proxy.http import upstream_client

    provider = GenericProvider(base_url=f"{origin}/v1/messages")
    client = upstream_client()

    async def stream() -> None:
        url = provider.url("/v1/messages", stream=True)
        async with client.stream("POST", url, json=BODY, headers=provider.headers()) as response:
            async for _ in response.aiter_text():
                pass

    return stream, SimpleNamespace(close=client.aclose)


@pytest.mark.parametrize("mode", [_transparent, _gateway], ids=["transparent", "gateway"])
async def test_every_stream_is_open_upstream_at_once(mode: Any) -> None:
    upstream = Upstream()
    sock = socket.create_server(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(upstream, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    stream, owner = mode(f"http://127.0.0.1:{sock.getsockname()[1]}")
    try:
        await asyncio.gather(*(stream() for _ in range(STREAMS)))
    finally:
        await owner.close()
        server.should_exit = True
        await serving
        sock.close()

    assert upstream.peak == STREAMS
