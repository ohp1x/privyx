"""A body with nothing to mask or restore streams through the proxy, both ways.

Read whole first, a download sat in memory and reached the client only after
its last byte, and an upload did the same on the way out.  Each test only
releases the rest of a body once the proxy has passed its first bytes on, so a
proxy that buffers never finishes.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from privyx.config.loader import load_config
from privyx.core.builder import build_engine
from privyx.core.engine import PrivacyEngine
from privyx.gateway.transparent import create_transparent_app
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

CHUNK = b"x" * 65536


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("upstream_fails", [False, True])
async def test_unknown_route_audit_never_records_client_content(
    allowed: bool, upstream_fails: bool
) -> None:
    trail = io.StringIO()
    audit = AuditLogger(trail)
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if upstream_fails:
            raise httpx.ConnectError("private-error-canary", request=request)
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        proxy = TransparentProxy(
            _engine(),
            origin="https://up.test",
            client=client,
            audit=audit,
            passthrough_unknown=allowed,
        )
        result = await proxy.handle(
            method="POST",
            path="files/path-canary@example.test",
            headers={"authorization": "Bearer credential-canary", "x-secret": "header-canary"},
            body=b"payload-canary",
            query_params={"secret": "query-canary"},
        )
        if result.stream is not None:
            assert b"".join([chunk async for chunk in result.stream]) == b"ok"
        expected_status = (502 if upstream_fails else 200) if allowed else 403
        assert result.status_code == expected_status
        assert len(seen) == int(allowed)
    records = [json.loads(line) for line in trail.getvalue().splitlines()]
    decision = records[0]
    assert decision["event"] == ("proxy.passthrough" if allowed else "proxy.blocked")
    assert decision["reason"] == "unknown_route"
    assert decision["body_masked"] is False
    assert decision["request_id"]
    assert all(r["request_id"] == decision["request_id"] for r in records)
    assert "canary" not in trail.getvalue()


async def test_strict_preset_blocks_unknown_body_without_reading_or_forwarding() -> None:
    from pathlib import Path

    settings = load_config(Path(__file__).parents[2] / "configs" / "strict.yaml")
    settings.vault.type = "memory"  # route behavior does not need a persistent test database
    engine, close = await build_engine(settings)

    async def unread_body() -> AsyncIterator[bytes]:
        raise AssertionError("blocked body must not be read")
        yield b""  # pragma: no cover

    def upstream(_: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked request must not reach upstream")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            proxy = TransparentProxy(
                engine,
                origin="https://up.test",
                client=client,
                routes=settings.proxy.routes,
                passthrough_unknown=settings.proxy.passthrough_unknown,
            )
            result = await proxy.handle(
                method="POST", path="v1/embeddings", headers={}, body=unread_body()
            )
            assert result.status_code == 403
    finally:
        await close()


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
