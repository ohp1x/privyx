"""A body with nothing to mask or restore streams through the proxy, both ways.

Read whole first, a download sat in memory and reached the client only after
its last byte, and an upload did the same on the way out.  Each test only
releases the rest of a body once the proxy has passed its first bytes on, so a
proxy that buffers never finishes.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import uvicorn

from privyx.core.engine import PrivacyEngine
from privyx.gateway.transparent import create_transparent_app
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

CHUNK = b"x" * 65536


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


async def test_a_download_reaches_the_client_as_it_arrives() -> None:
    relayed = asyncio.Event()

    async def download() -> AsyncIterator[bytes]:
        yield CHUNK
        await relayed.wait()
        yield CHUNK

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=download()))
    )
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=client)

    result = await asyncio.wait_for(
        proxy.handle(method="GET", path="v1/files/f/content", headers={}, body=b""), 5
    )
    assert result.stream is not None
    chunks = aiter(result.stream)
    assert await asyncio.wait_for(anext(chunks), 5) == CHUNK
    relayed.set()
    assert [chunk async for chunk in chunks] == [CHUNK]


async def test_an_upload_goes_upstream_as_it_arrives_with_its_length() -> None:
    received = asyncio.Event()
    seen: list[dict[str, Any]] = []

    async def upstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        size, more = 0, True
        while more:
            message = await receive()
            size += len(message.get("body", b""))
            more = message.get("more_body", False)
            if size:
                received.set()
        headers = dict(scope["headers"])
        seen.append(
            {
                "size": size,
                "content-length": headers.get(b"content-length"),
                "transfer-encoding": headers.get(b"transfer-encoding"),
            }
        )
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def upload() -> AsyncIterator[bytes]:
        yield CHUNK
        await asyncio.wait_for(received.wait(), 5)
        yield CHUNK

    sock = socket.create_server(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(upstream, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    proxy = TransparentProxy(_engine(), origin=f"http://127.0.0.1:{sock.getsockname()[1]}")
    app = create_transparent_app(_engine(), proxy)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            length = str(2 * len(CHUNK))
            await client.post("/v1/files", content=upload(), headers={"content-length": length})
            await client.get("/v1/models")
    finally:
        await proxy.close()
        server.should_exit = True
        await serving
        sock.close()

    assert seen == [
        {"size": 2 * len(CHUNK), "content-length": length.encode(), "transfer-encoding": None},
        # No body stays no body, rather than an empty chunked one.
        {"size": 0, "content-length": None, "transfer-encoding": None},
    ]
