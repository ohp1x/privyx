"""Every way a reply ends closes its upstream response and ends its session once.

The app is called as an ASGI server calls it, with a ``send`` that blocks,
fails, or is cancelled at a chosen message.  Starlette closes no streamed reply
then, started or not, so the adapter does; and a batch body still being read
when the request is cancelled is closed by the pipeline.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.observability import tracing
from privyx.operators._pseudonym import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.proxy import Pipeline
from privyx.proxy._asgi import create_app
from privyx.testing import audit_to
from privyx.vaults import MemoryVault

BODY = {"messages": [{"role": "user", "content": "mail alice@example.com"}]}
# What the upstream sends first, then nothing for a minute.
FIRST = {
    "batch": json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode(),
    "sse": b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
    "relay": b"hi",
}
TYPES = {"batch": "application/json", "sse": "text/event-stream", "relay": "application/zip"}


class _Upstream(httpx.AsyncByteStream):
    """A response body: its first chunk, then a stall; it counts what is left open."""

    def __init__(self, kind: str, stalled: asyncio.Event, open_bodies: list[int]) -> None:
        self._kind, self._stalled, self._open = kind, stalled, open_bodies
        self._open[0] += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield FIRST[self._kind]
        self._stalled.set()
        await asyncio.sleep(60)

    async def aclose(self) -> None:
        self._open[0] -= 1


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The finished spans; ``None`` without the otel extra."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:
        yield None
        return
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    yield exporter


# (kind, ending, spec): `send` blocks at message n and the request is cancelled
# ("cancel n"), or it raises at message n ("fail n"); "cancel" alone waits for
# the upstream to stall after its first chunk, which the client got.
ROWS = [
    ("batch", "cancel", "2.4"),  # while the body is read
    *[
        (kind, ending, spec)
        for kind in ("sse", "relay")
        for ending, spec in [
            ("cancel 1", "2.4"),  # while the headers go out
            ("fail 1", "2.4"),
            ("fail 1", "2.3"),
            ("fail 2", "2.4"),  # on the first chunk
            ("fail 2", "2.3"),
            ("cancel", "2.4"),  # after one chunk
        ]
    ],
]


@pytest.mark.parametrize(("kind", "ending", "spec"), ROWS)
async def test_the_upstream_response_is_closed(
    kind: str, ending: str, spec: str, spans: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stalled, blocked = asyncio.Event(), asyncio.Event()
    open_bodies = [0]

    def upstream(request: httpx.Request) -> httpx.Response:
        body = _Upstream(kind, stalled, open_bodies)
        return httpx.Response(200, headers={"content-type": TYPES[kind]}, stream=body)

    trail = io.StringIO()
    audit = audit_to(trail)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(Pipeline(engine, origin="https://up.test", client=client, audit=audit))

    body = json.dumps({**BODY, "stream": kind == "sse"}).encode()
    path = "/v1/files/f/content" if kind == "relay" else "/v1/chat/completions"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("proxy.test", 80),
        "client": ("127.0.0.1", 50000),
    }
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # the client never leaves on its own
        raise AssertionError("unreachable")

    action, _, at = ending.partition(" ")
    sent = 0

    async def send(message: dict[str, Any]) -> None:
        nonlocal sent
        sent += 1
        if at and sent == int(at):
            if action == "fail":
                raise OSError("the client is gone")
            blocked.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(app(scope, receive, send))
    if action == "cancel":
        await (blocked if at else stalled).wait()
        task.cancel()
    with pytest.raises(BaseException):  # noqa: B017 - cancelled, or the failed send
        await task

    assert open_bodies == [0]
    records = [json.loads(line) for line in trail.getvalue().splitlines()]
    assert [r["event"] for r in records].count("session.deleted") == 1
    if spans is not None:
        assert "privyx.request" in {span.name for span in spans.get_finished_spans()}
    await client.aclose()
    gc.collect()
    await asyncio.sleep(0)  # what the collector found would be closed now
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
