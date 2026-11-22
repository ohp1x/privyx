"""``Privyx.asgi()``: the edge middleware around the router, and its lifespan."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from privyx.app import Privyx  # noqa: E402


def _recorder(calls: list[str]) -> Any:
    """A middleware factory whose middleware records its ``name`` on each HTTP request."""

    def factory(app: Any, *, name: str) -> Any:
        async def middleware(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                calls.append(name)
            await app(scope, receive, send)

        return middleware

    return factory


async def test_middleware_runs_from_the_highest_priority_in() -> None:
    calls: list[str] = []
    px = Privyx(features={"audit": False})
    record = _recorder(calls)
    px.add_middleware(record, name="inner", priority=-5)
    px.add_middleware(record, name="first-of-ten", priority=10)
    px.add_middleware(record, name="default")
    px.add_middleware(record, name="second-of-ten", priority=10)

    transport = httpx.ASGITransport(app=px.asgi())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")  # every route goes through the edge

    assert response.json() == {"status": "ok"}
    assert calls == ["first-of-ten", "second-of-ten", "default", "inner"]


async def _lifespan(app: Any) -> AsyncIterator[None]:
    """Run ``app``'s lifespan around the body, as a server does."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put)
    )
    assert (await asyncio.wait_for(outbox.get(), 10))["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outbox.get(), 10))["type"] == "lifespan.shutdown.complete"
        await task


async def test_a_stream_reaches_the_client_as_it_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan opens the pipeline, and a stream is relayed event by event, not buffered."""
    released = asyncio.Event()

    class Upstream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
            await released.wait()  # the rest only once the client has the first event
            yield b"data: [DONE]\n\n"

    def reply(request: httpx.Request) -> httpx.Response:
        sse = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=sse, stream=Upstream())

    monkeypatch.setattr(
        "privyx.proxy._pipeline.upstream_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )
    calls: list[str] = []
    px = Privyx(features={"audit": False}, vault={"type": "memory"}, anchor={"secret": ""})
    px.add_middleware(_recorder(calls), name="edge")
    app = px.asgi()

    body = json.dumps({"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    requests = [{"type": "http.request", "body": body.encode(), "more_body": False}]
    sent: list[dict[str, Any]] = []
    first = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requests:
            return requests.pop(0)
        await asyncio.Event().wait()  # no disconnect: the client stays
        raise AssertionError

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    async for _ in _lifespan(app):
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first.wait(), 10)
        assert not released.is_set()  # the first event came while the upstream held the rest
        released.set()
        await asyncio.wait_for(task, 10)

    assert sent[0]["status"] == 200
    streamed = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b'"one"' in streamed and streamed.endswith(b"data: [DONE]\n\n")
    assert calls == ["edge"]
