"""A client that drops a stream must not leave its ephemeral session behind.

Each app runs under a real uvicorn server: only there does Starlette cancel the
stream when the client disconnects (uvicorn reports ASGI 2.3), which httpx's
ASGITransport never does.  The vault is SQLite because the in-memory vault never
suspends, so the cancellation would not reach its deletion.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

pytest.importorskip("aiosqlite")

from privyx.config.schema import Settings  # noqa: E402
from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.gateway.server import Gateway  # noqa: E402
from privyx.gateway.transparent import create_transparent_app  # noqa: E402
from privyx.observability.audit import AuditLogger  # noqa: E402
from privyx.privacy.detector.builtin import RegexDetector  # noqa: E402
from privyx.privacy.operator.pseudonym import PseudonymOperator  # noqa: E402
from privyx.privacy.policy.default import DefaultPolicy  # noqa: E402
from privyx.providers.generic import GenericProvider  # noqa: E402
from privyx.proxy.transparent import TransparentProxy  # noqa: E402
from privyx.vault.sqlite import SQLiteVault  # noqa: E402

STREAMS = 80
BODY = {"stream": True, "messages": [{"role": "user", "content": "mail alice@example.com"}]}
FRAME = (
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
)


def _upstream(headers_sent: asyncio.Event) -> httpx.AsyncClient:
    """Answer once ``headers_sent`` is set with one frame, then never finish.

    Any path but ``/v1/messages`` gets a download, which is relayed unrestored.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        await headers_sent.wait()

        async def body() -> AsyncIterator[bytes]:
            yield FRAME
            await asyncio.sleep(60)

        sse = request.url.path == "/v1/messages"
        content_type = "text/event-stream" if sse else "application/octet-stream"
        return httpx.Response(200, headers={"content-type": content_type}, content=body())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _app(mode: str, engine: PrivacyEngine, audit: AuditLogger, upstream: httpx.AsyncClient) -> Any:
    if mode == "transparent":
        proxy = TransparentProxy(engine, origin="http://up.test", client=upstream, audit=audit)
        return create_transparent_app(engine, proxy)
    url = "http://up.test/v1/messages"
    provider = GenericProvider(base_url=url, client=upstream)
    settings = Settings(provider={"type": "anthropic", "base_url": url})
    return Gateway(engine, provider, settings, audit=audit).app


async def _drop_after_first_chunk(client: httpx.AsyncClient, url: str) -> None:
    async with client.stream("POST", url, json=BODY) as response:
        async for _ in response.aiter_bytes():
            break


async def _drop_before_headers(client: httpx.AsyncClient, url: str) -> None:
    with contextlib.suppress(httpx.ReadTimeout):
        await client.post(url, json=BODY, timeout=0.2)


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("transparent", "/v1/messages"),
        ("gateway", "/v1/messages"),
        ("transparent", "/v1/files/f/content"),
    ],
    ids=["transparent", "gateway", "transparent-download"],
)
@pytest.mark.parametrize("early", [False, True], ids=["mid-stream", "before-headers"])
async def test_dropped_streams_leave_no_session(mode: str, path: str, early: bool) -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    vault = SQLiteVault(":memory:")
    await vault.connect()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=audit,
    )
    headers_sent = asyncio.Event()
    if not early:
        headers_sent.set()
    app = _app(mode, engine, audit, _upstream(headers_sent))
    # Already listening, so connections queue until the server accepts them.
    sock = socket.create_server(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        url = f"http://127.0.0.1:{sock.getsockname()[1]}{path}"
        drop = _drop_before_headers if early else _drop_after_first_chunk
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=None)) as client:
            await asyncio.gather(*(drop(client, url) for _ in range(STREAMS)))
        headers_sent.set()  # the upstream answers after every client has gone

        for _ in range(200):  # cleanup follows the disconnect; give it time
            if not await vault.list_sessions():
                break
            await asyncio.sleep(0.05)
        left = len(await vault.list_sessions())
    finally:
        server.should_exit = True
        await serving
        sock.close()
        await vault.close()

    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    responses = [r for r in records if r["event"] == "proxy.response"]
    assert left == 0
    assert sum(r["event"] == "session.deleted" for r in records) == STREAMS
    assert len(responses) == STREAMS
    assert all(r["aborted"] for r in responses)
    assert not any(r["event"] == "proxy.error" for r in records)
