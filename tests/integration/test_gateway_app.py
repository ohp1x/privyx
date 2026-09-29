"""End-to-end tests for the gateway app (``--gateway``) through its ASGI app.

The gateway exposes *its own* endpoints and posts every one of them to the
single upstream endpoint from ``provider.base_url``, used verbatim — the
property that lets an upstream carry a base path.  These tests pin both halves:
the route map comes from ``proxy.routes`` (so ``/v1/messages`` is served, not
just ``/v1/chat/completions``), and the wire schema used to restore a response
comes from the route.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from privyx.config.schema import Settings  # noqa: E402
from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.core.errors import DetectorError, VaultError  # noqa: E402
from privyx.gateway.server import Gateway  # noqa: E402
from privyx.observability.audit import AuditLogger  # noqa: E402
from privyx.privacy.detector.builtin import RegexDetector  # noqa: E402
from privyx.privacy.operator.pseudonym import PseudonymOperator  # noqa: E402
from privyx.privacy.policy.default import DefaultPolicy  # noqa: E402
from privyx.providers.generic import GenericProvider  # noqa: E402
from privyx.vault.memory import MemoryVault  # noqa: E402

EMAIL = "alice@example.com"
UPSTREAM = "https://up.test/anthropic/v1/messages"

seen: list[httpx.Request] = []


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def _upstream_client() -> httpx.AsyncClient:
    """Echo the last message back in the Anthropic batch shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client(
    engine: PrivacyEngine, audit: AuditLogger | None = None
) -> tuple[httpx.AsyncClient, Any]:
    provider = GenericProvider(
        base_url=UPSTREAM,
        headers={"x-api-key": "sk-test"},
        client=_upstream_client(),
    )
    settings = Settings(provider={"type": "anthropic", "base_url": UPSTREAM})
    gateway = Gateway(engine=engine, provider=provider, settings=settings, audit=audit)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ), gateway


def _stream_client(engine: PrivacyEngine, audit: AuditLogger) -> tuple[httpx.AsyncClient, Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        frames = (
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            )
            + "\n\n"
            + 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=frames.encode()
        )

    provider = GenericProvider(
        base_url=UPSTREAM,
        headers={"x-api-key": "sk-test"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    settings = Settings(provider={"type": "anthropic", "base_url": UPSTREAM})
    gateway = Gateway(engine=engine, provider=provider, settings=settings, audit=audit)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ), gateway


def _audited_engine(
    buf: io.StringIO, vault: MemoryVault | None = None
) -> tuple[PrivacyEngine, AuditLogger]:
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault or MemoryVault(),
        audit=audit,
    )
    return engine, audit


def _records(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def _session_id(response: httpx.Response) -> str:
    return cast(str, response.headers["x-privyx-session"])


@pytest.mark.asyncio
async def test_messages_route_masks_and_restores() -> None:
    """``POST /v1/messages`` is served, pseudonymized, and restored as Anthropic."""
    seen.clear()
    client, _ = _client(_engine())
    async with client:
        response = await client.post(
            "/v1/messages", json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )

    assert response.status_code == 200
    # Restored with the Anthropic walker (content[].text), not the OpenAI one.
    assert response.json()["content"][0]["text"] == f"mail {EMAIL}"
    assert response.headers["x-privyx-session"]

    # The upstream never saw the real address, and the base path survived.
    sent = json.loads(seen[-1].content)
    assert EMAIL not in sent["messages"][-1]["content"]
    assert str(seen[-1].url) == UPSTREAM


@pytest.mark.asyncio
async def test_metrics_count_masked_entities() -> None:
    """``GET /metrics`` serves the totals of the engine's and gateway's shared audit logger."""
    audit = AuditLogger(None)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,  # `privyx proxy` builds the engine and gateway around one logger
    )
    client, _ = _client(engine, audit)
    async with client:
        await client.post(
            "/v1/messages", json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )
        text = (await client.get("/metrics")).text

    assert 'privyx_entities_masked_total{entity_type="EMAIL"} 1' in text
    assert 'privyx_audit_events_total{event="proxy.response"} 1' in text
    assert EMAIL not in text


@pytest.mark.asyncio
async def test_a_masking_failure_is_a_503_audited_and_never_forwarded() -> None:
    class _Broken:
        name = "broken"

        async def detect(self, text: str, context: Any) -> Any:
            raise DetectorError(f"boom while reading {text}")

    seen.clear()
    buf = io.StringIO()
    audit = AuditLogger(buf)
    vault = MemoryVault()
    engine = PrivacyEngine(
        detector=_Broken(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=audit,
    )
    client, _ = _client(engine, audit)
    async with client:
        response = await client.post(
            "/v1/messages", json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "privyx_scan_failed"
    assert EMAIL not in response.text
    assert seen == []
    errors = [json.loads(line) for line in buf.getvalue().splitlines() if "proxy.error" in line]
    assert [(e["phase"], e["error_type"]) for e in errors] == [("transform", "DetectorError")]
    # The ephemeral session is known before masking starts, so it is cleaned up.
    assert await vault.list_sessions() == []


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "nth", "stream", "forwarded"),
    [
        ("get", 1, False, False),  # looking the session up
        ("create", 1, False, False),  # creating it
        ("save", 1, False, False),  # saving the request's pseudonyms
        ("get", 4, True, False),  # reading them to restore a stream, before the upstream call
        ("get", 4, False, True),  # ... or a batch reply, after it
    ],
)
async def test_a_vault_failure_is_a_503_audited(
    method: str, nth: int, stream: bool, forwarded: bool
) -> None:
    seen.clear()
    buf = io.StringIO()
    engine, audit = _audited_engine(buf, _flaky_vault(method, nth))
    client, _ = (_stream_client if stream else _client)(engine, audit)
    async with client:
        response = await client.post(
            "/v1/messages",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
            headers={"x-privyx-session": "ses_sticky"},  # an ephemeral one skips the vault
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "privyx_vault_unavailable"
    assert bool(seen) is forwarded
    errors = [json.loads(line) for line in buf.getvalue().splitlines() if "proxy.error" in line]
    assert [(e["phase"], e["error_type"]) for e in errors] == [("vault", "VaultError")]


@pytest.mark.asyncio
async def test_routes_come_from_config() -> None:
    """Configured chat paths are registered; auxiliary and unknown ones 404."""
    client, gateway = _client(_engine())
    assert gateway.routes == {
        "/v1/chat/completions": "openai",
        "/v1/messages": "anthropic",
        "/v1/responses": "responses",
    }
    async with client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.post("/v1/embeddings", json={})).status_code == 404
        # Never forwarded to the chat endpoint as a billed completion.
        assert (await client.post("/v1/messages/count_tokens", json={})).status_code == 404


@pytest.mark.asyncio
async def test_gateway_records_the_time_spent_masking() -> None:
    buf = io.StringIO()
    engine, audit = _audited_engine(buf)
    client, _ = _client(engine, audit)

    async with client:
        await client.post(
            "/v1/messages", json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )

    request = next(r for r in _records(buf) if r["event"] == "proxy.request")
    assert 0 <= request["transform_ms"] <= request["duration_ms"]


@pytest.mark.asyncio
async def test_gateway_ephemeral_batch_session_is_deleted() -> None:
    buf = io.StringIO()
    engine, audit = _audited_engine(buf)
    client, _ = _client(engine, audit)

    async with client:
        response = await client.post(
            "/v1/messages", json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )

    sid = _session_id(response)
    assert response.status_code == 200
    assert await engine.vault.get(sid) is None
    records = _records(buf)
    deleted = next(r for r in records if r["event"] == "session.deleted")
    assert deleted["session_id"] == sid
    assert deleted["mapping_count"] == 1


@pytest.mark.asyncio
async def test_gateway_sticky_session_is_retained() -> None:
    buf = io.StringIO()
    engine, audit = _audited_engine(buf)
    client, _ = _client(engine, audit)

    async with client:
        first = await client.post(
            "/v1/messages",
            headers={"x-privyx-session": "ses_sticky"},
            json={"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )

    assert first.status_code == 200
    assert await engine.vault.get("ses_sticky") is not None
    assert not any(r["event"] == "session.deleted" for r in _records(buf))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["batch", "stream"])
async def test_an_ephemeral_request_never_touches_the_vault(stream: bool) -> None:
    calls: list[str] = []
    vault = MemoryVault()
    for name in ("create", "get", "save", "delete", "list_sessions"):
        setattr(vault, name, lambda *args, _name=name: calls.append(_name))
    buf = io.StringIO()
    engine, audit = _audited_engine(buf, vault)
    client, _ = (_stream_client if stream else _client)(engine, audit)
    async with client:
        response = await client.post(
            "/v1/messages",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )

    assert calls == []
    assert response.status_code == 200
    assert EMAIL in response.text  # masked and restored with the in-memory mapping
    lifecycle = [r for r in _records(buf) if r["event"] in ("session.created", "session.deleted")]
    assert [r["event"] for r in lifecycle] == ["session.created", "session.deleted"]
    assert lifecycle[1]["mapping_count"] == 1


@pytest.mark.asyncio
async def test_gateway_stream_cleanup_deletes_after_drain() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    vault = MemoryVault()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=audit,
    )

    client, _ = _stream_client(engine, audit)

    async with client:
        async with client.stream(
            "POST",
            "/v1/messages",
            json={"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        ) as response:
            sid = response.headers["x-privyx-session"]
            body = "".join([chunk async for chunk in response.aiter_text()])

    assert response.status_code == 200
    assert EMAIL in body
    assert await vault.get(sid) is None
    assert any(r["event"] == "session.deleted" for r in _records(buf))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_error_is_relayed(stream: bool) -> None:
    """An upstream 4xx reaches the client with its status and body, not a 500."""
    error = {"error": {"message": "bad model"}}
    vault = MemoryVault()
    engine = PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )
    provider = GenericProvider(
        base_url=UPSTREAM,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(400, json=error))
        ),
    )
    gateway = Gateway(engine=engine, provider=provider, settings=Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )

    assert response.status_code == 400
    assert response.json() == error
    assert vault._sessions == {}  # ephemeral session still cleaned up


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("failure", "status", "kind"),
    [
        (httpx.ConnectError, 502, "unreachable"),
        (httpx.ReadTimeout, 504, "timeout"),
        (httpx.PoolTimeout, 503, "busy"),
    ],
)
async def test_no_upstream_response_is_named_by_status_and_type(
    failure: type[Exception], status: int, kind: str, stream: bool, caplog: Any
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure("")  # a timeout's message is empty

    provider = GenericProvider(
        base_url=UPSTREAM, client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
    )
    gateway = Gateway(engine=_engine(), provider=provider, settings=Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        )

    error = response.json()["error"]
    assert response.status_code == status
    assert error["type"] == f"privyx_upstream_{kind}"
    assert error["message"].endswith(f"({failure.__name__})")
    assert response.headers.get("retry-after") == ("10" if status == 503 else None)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]


@pytest.mark.parametrize(
    "body", [b"", b"mail alice@example.com", b'[{"content": "mail alice@example.com"}]']
)
async def test_a_body_that_is_not_a_json_object_is_a_400(body: bytes) -> None:
    seen.clear()
    client, _ = _client(_engine())
    async with client:
        response = await client.post(
            "/v1/messages", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "privyx_invalid_request"
    assert not seen
