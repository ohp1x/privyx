"""OpenTelemetry spans per request: off by default, PII-free when on."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import ConfigError
from privyx.observability import tracing
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Only these tests need the otel extra; the rest run without it.
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = memory.InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    yield exporter


def _proxy() -> TransparentProxy:
    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        if body.get("stream"):
            sse = f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return TransparentProxy(engine, origin="https://up.test", client=client)


@pytest.mark.parametrize("stream", [False, True])
async def test_a_request_gets_a_span_tree_without_payload_text(spans: Any, stream: bool) -> None:
    result = await _proxy().handle(
        method="POST",
        path="v1/chat/completions",
        headers={"traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01"},
        body=json.dumps(
            {"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        ).encode(),
    )
    if result.stream is not None:
        _ = [chunk async for chunk in result.stream]

    finished = {span.name: span for span in spans.get_finished_spans()}
    assert set(finished) == {
        "privyx.request",
        "privyx.transform",
        "privyx.upstream",
        "privyx.restore",
    }
    root = finished["privyx.request"]
    assert format(root.context.trace_id, "032x") == TRACE_ID  # the client's trace
    for name in ("privyx.transform", "privyx.upstream", "privyx.restore"):
        assert finished[name].parent.span_id == root.context.span_id
    assert root.attributes["http.route"] == "/v1/chat/completions"
    assert root.attributes["http.response.status_code"] == 200
    assert root.attributes["privyx.stream"] is stream
    assert finished["privyx.restore"].attributes["privyx.restored"] >= 1
    everything = json.dumps([dict(span.attributes) for span in finished.values()])
    assert EMAIL not in everything and "PRIVYX_" not in everything


@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/files"])
async def test_a_reply_closed_before_it_is_read_still_ends_its_span(spans: Any, path: str) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files":  # nothing to restore: relayed
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="raw")
        sse = "data: {}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy = TransparentProxy(engine, origin="https://up.test", client=client)
    body = json.dumps({"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    result = await proxy.handle(
        method="POST" if path.endswith("completions") else "GET",
        path=path,
        headers={},
        body=body.encode() if path.endswith("completions") else b"",
    )
    assert result.stream is not None
    await result.stream.aclose()  # the client went away before the first byte

    root = next(s for s in spans.get_finished_spans() if s.name == "privyx.request")
    assert root.attributes["privyx.aborted"] is True


async def test_a_failed_restore_ends_its_span_and_records_the_503(spans: Any) -> None:
    from privyx.core.errors import VaultError

    class FailingVault(MemoryVault):
        broken = False

        async def get(self, session_id: str) -> Any:
            if self.broken:
                raise VaultError("down")
            return await super().get(session_id)

    vault = FailingVault()

    def upstream(request: httpx.Request) -> httpx.Response:
        vault.broken = True  # the vault fails between the request and the reply
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    engine = PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy = TransparentProxy(engine, origin="https://up.test", client=client)
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()

    result = await proxy.handle(
        method="POST", path="v1/chat/completions", headers={"x-privyx-session": "s"}, body=body
    )

    assert result.status_code == 503
    finished = {s.name: s for s in spans.get_finished_spans()}
    assert finished["privyx.restore"].attributes["error.type"] == "VaultError"
    root = finished["privyx.request"]
    assert root.attributes["http.response.status_code"] == 503
    assert root.status.status_code.name == "ERROR"


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "text/event-stream"])
async def test_an_upstream_5xx_marks_the_request_span_failed(spans: Any, content_type: str) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, headers={"content-type": content_type}, text="bad gateway")

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy = TransparentProxy(engine, origin="https://up.test", client=client)
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()

    result = await proxy.handle(method="POST", path="v1/chat/completions", headers={}, body=body)
    if result.stream is not None:
        _ = [chunk async for chunk in result.stream]

    root = next(s for s in spans.get_finished_spans() if s.name == "privyx.request")
    assert root.attributes["http.response.status_code"] == 502
    assert root.status.status_code.name == "ERROR"


def test_tracing_is_off_and_not_imported_by_default() -> None:
    # FastAPI imports the OpenTelemetry API itself; the SDK and exporter are ours.
    code = (
        "import sys, privyx.proxy.transparent, privyx.gateway.transparent, privyx.cli.main;"
        "print(sorted(m for m in sys.modules"
        " if m.startswith(('opentelemetry.sdk', 'opentelemetry.exporter'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
    assert tracing.start("privyx.request") is None


def test_enabling_tracing_without_the_extra_says_how_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    with pytest.raises(ConfigError, match=r"privyx\[otel\]"):
        tracing.configure(True)


def test_turning_tracing_off_on_reload_stops_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(tracing, "_provider", None)

    tracing.configure(True)
    assert tracing.start("privyx.request") is not None
    tracing.configure(False)  # what --reload does after `enabled: false`
    assert tracing.start("privyx.request") is None
    tracing.configure(True)
    assert tracing.start("privyx.request") is not None
    tracing.configure(False)
