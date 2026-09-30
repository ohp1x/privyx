"""End-to-end tests for the transparent proxy through its ASGI app.

These drive :func:`create_transparent_app` with an in-process ASGI transport, so
the FastAPI catch-all route, ``/health``, and the ``ProxyResponse`` →
``Response``/``StreamingResponse`` conversion are all exercised — the layer the
direct ``TransparentProxy.handle`` tests skip.  The upstream is still a mock.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.core.errors import DetectorError, VaultError  # noqa: E402
from privyx.gateway.transparent import create_transparent_app  # noqa: E402
from privyx.observability.audit import AuditLogger  # noqa: E402
from privyx.privacy.detector.builtin import RegexDetector  # noqa: E402
from privyx.privacy.operator.pseudonym import PseudonymOperator  # noqa: E402
from privyx.privacy.policy.default import DefaultPolicy  # noqa: E402
from privyx.proxy.transparent import TransparentProxy  # noqa: E402
from privyx.vault.memory import MemoryVault  # noqa: E402

EMAIL = "alice@example.com"


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def _upstream(seen: list[bytes] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.content)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/messages/count_tokens":
            return httpx.Response(200, json={"input_tokens": 12})
        if request.url.path == "/v1/responses":
            return _responses_reply(body)
        if request.url.path == "/v1/chat/completions":
            content = body["messages"][-1]["content"]
            if body.get("stream"):
                frames = (
                    f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"
                    "data: [DONE]\n\n"
                )
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=frames.encode()
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
                headers={"x-request-id": "req-42"},
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m <PRIVYX_EMAIL_9>"}]})
        return httpx.Response(404, json={"error": "nope"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _responses_reply(body: dict[str, Any]) -> httpx.Response:
    """Echo the last input_text as a Responses message, batch or streamed."""
    text = body["input"][-1]["content"][0]["text"]
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    if not body.get("stream"):
        return httpx.Response(200, json={"id": "resp_1", "object": "response", "output": [item]})
    ids = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    half = len(text) - 6  # splits the token across two deltas
    events = [
        {"type": "response.output_text.delta", "delta": text[:half], **ids},
        {"type": "response.output_text.delta", "delta": text[half:], **ids},
        {"type": "response.output_text.done", "text": text, **ids},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "resp_1", "output": [item]}},
    ]
    frames = "".join(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n" for ev in events)
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=frames.encode()
    )


def _app(seen: list[bytes] | None = None) -> Any:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_upstream(seen))
    return create_transparent_app(engine, proxy)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test")


async def test_health() -> None:
    async with _client(_app()) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_chat_paths_without_v1_are_masked() -> None:
    """The OpenAI SDK calls these when its base URL lacks /v1; they are not forwarded as is."""
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={})

    engine = _engine()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = TransparentProxy(engine, origin="https://up.test", client=upstream)
    async with _client(create_transparent_app(engine, proxy)) as client:
        chat = {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        await client.post("/chat/completions", json=chat)
        await client.post("/responses", json={"input": f"mail {EMAIL}"})

    assert [path for path, _ in seen] == ["/chat/completions", "/responses"]
    for _, body in seen:
        assert EMAIL.encode() not in body
        assert b"PRIVYX_EMAIL" in body


async def test_metrics_count_proxied_exchanges() -> None:
    async with _client(_app()) as client:
        await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'privyx_audit_events_total{event="proxy.response"} 1' in response.text


class _BrokenDetector:
    name = "broken"

    async def detect(self, text: str, context: Any) -> Any:
        raise DetectorError(f"boom while reading {text}")


async def test_a_masking_failure_is_a_503_audited_and_never_forwarded() -> None:
    seen: list[bytes] = []
    buf = io.StringIO()
    audit = AuditLogger(buf)
    vault = MemoryVault()
    engine = PrivacyEngine(
        detector=_BrokenDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=audit,
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_upstream(seen), audit=audit)
    async with _client(create_transparent_app(engine, proxy)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )
        metrics = (await client.get("/metrics")).text

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "privyx_scan_failed"
    assert EMAIL not in response.text  # the exception message is not echoed
    assert seen == []  # fail-closed: nothing reached the upstream
    errors = [json.loads(line) for line in buf.getvalue().splitlines() if "proxy.error" in line]
    assert [(e["phase"], e["error_type"]) for e in errors] == [("transform", "DetectorError")]
    assert 'privyx_proxy_errors_total{phase="transform"} 1' in metrics
    assert await vault.list_sessions() == []  # the ephemeral session was cleaned up


def _flaky_vault(method: str, nth: int) -> MemoryVault:
    """A vault whose ``nth`` call of ``method`` fails, like a Redis that stops answering."""
    vault = MemoryVault()
    real = getattr(vault, method)
    calls = 0

    async def flaky(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == nth:
            raise VaultError("redis vault: Timeout reading from socket")
        return await real(*args)

    setattr(vault, method, flaky)
    return vault


@pytest.mark.parametrize(
    ("method", "nth", "stream", "forwarded"),
    [
        ("get", 1, False, False),  # looking the session up
        ("create", 1, False, False),  # creating it
        ("save", 1, False, False),  # saving the request's pseudonyms
        ("get", 3, False, True),  # reading them to restore the reply
        ("get", 3, True, True),  # ... of a stream, before its 200 goes out
    ],
)
async def test_a_vault_failure_is_a_503_audited(
    method: str, nth: int, stream: bool, forwarded: bool, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[bytes] = []
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=_flaky_vault(method, nth),
        audit=audit,
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_upstream(seen), audit=audit)
    async with _client(create_transparent_app(engine, proxy)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
            headers={"x-privyx-session": "ses_sticky"},  # an ephemeral one skips the vault
        )
        metrics = (await client.get("/metrics")).text

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "privyx_vault_unavailable"
    assert bool(seen) is forwarded
    errors = [json.loads(line) for line in buf.getvalue().splitlines() if "proxy.error" in line]
    assert [(e["phase"], e["error_type"]) for e in errors] == [("vault", "VaultError")]
    assert 'privyx_proxy_errors_total{phase="vault"} 1' in metrics
    # One WARNING line; the traceback only at DEBUG.
    assert any("session vault" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]


@pytest.mark.parametrize("stream", [False, True], ids=["batch", "stream"])
async def test_an_ephemeral_request_never_touches_the_vault(stream: bool) -> None:
    calls: list[str] = []
    vault = MemoryVault()
    for name in ("create", "get", "save", "delete", "list_sessions"):
        setattr(vault, name, lambda *args, _name=name: calls.append(_name))
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=audit,
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_upstream(), audit=audit)
    async with _client(create_transparent_app(engine, proxy)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )

    assert calls == []
    assert response.status_code == 200
    assert EMAIL in response.text  # masked and restored with the in-memory mapping
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    lifecycle = [e for e in events if e["event"] in ("session.created", "session.deleted")]
    assert [e["event"] for e in lifecycle] == ["session.created", "session.deleted"]
    assert lifecycle[1]["mapping_count"] == 1


async def test_chat_batch_round_trip_through_app() -> None:
    async with _client(_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == f"mail {EMAIL}"
    assert response.headers["x-request-id"] == "req-42"
    assert "x-privyx-session" in response.headers


async def test_chat_stream_through_app() -> None:
    async with _client(_app()) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join([chunk async for chunk in response.aiter_text()])
    assert "<PRIVYX_" not in body
    assert EMAIL in body
    assert body.rstrip().endswith("data: [DONE]")


async def test_unrouted_path_passthrough_through_app() -> None:
    async with _client(_app()) as client:
        response = await client.get("/v1/models")
    assert response.json()["data"][0]["id"] == "m <PRIVYX_EMAIL_9>"


async def test_count_tokens_is_pseudonymized() -> None:
    seen: list[bytes] = []
    async with _client(_app(seen)) as client:
        response = await client.post(
            "/v1/messages/count_tokens?beta=true",
            json={"model": "m", "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )
    assert response.json() == {"input_tokens": 12}
    assert EMAIL.encode() not in seen[-1] and b"<PRIVYX_EMAIL_" in seen[-1]


def _responses_body(stream: bool) -> dict[str, Any]:
    return {
        "model": "m",
        "stream": stream,
        "instructions": f"help {EMAIL}",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": f"mail {EMAIL}"}]}],
    }


async def test_responses_batch_round_trip() -> None:
    seen: list[bytes] = []
    async with _client(_app(seen)) as client:
        response = await client.post("/v1/responses", json=_responses_body(stream=False))
    assert EMAIL.encode() not in seen[-1]
    assert response.json()["output"][0]["content"][0]["text"] == f"mail {EMAIL}"


async def test_responses_stream_round_trip() -> None:
    seen: list[bytes] = []
    async with _client(_app(seen)) as client:
        async with client.stream(
            "POST", "/v1/responses", json=_responses_body(stream=True)
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
    assert EMAIL.encode() not in seen[-1]
    assert "<PRIVYX_" not in body
    events = [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]
    deltas = [ev["delta"] for ev in events if ev["type"] == "response.output_text.delta"]
    assert "".join(deltas) == f"mail {EMAIL}"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == f"mail {EMAIL}"
