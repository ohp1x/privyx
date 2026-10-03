"""Integration tests for the transparent (drop-in) reverse proxy.

The upstream is an :class:`httpx.MockTransport` that echoes the request's last
user message back as the assistant reply, so a round-trip that returns the
original PII proves the request was pseudonymized upstream and the response
restored on the way back.  A capture list lets tests assert what the upstream
actually received.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

import httpx
import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import DetectorError
from privyx.observability.audit import AuditLogger
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
EMAIL2 = "bob@example.org"


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def _chunks(text: str, size: int = 4) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _mock_client(capture: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """An httpx client whose upstream echoes the last user message back."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path == "/v1/chat/completions":
            content = body["messages"][-1]["content"]
            if body.get("stream"):
                frames = (
                    "".join(
                        f"data: {json.dumps({'choices': [{'delta': {'content': part}}]})}\n\n"
                        for part in _chunks(content)
                    )
                    + "data: [DONE]\n\n"
                )
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=frames.encode()
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": content}}]},
                headers={"x-request-id": "req-1"},
            )

        if path == "/v1/messages":
            content = body["messages"][-1]["content"]
            return httpx.Response(200, json={"content": [{"type": "text", "text": content}]})

        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-x <PRIVYX_EMAIL_9>"}]})

        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _post_json(proxy: TransparentProxy, path: str, payload: dict[str, Any]) -> Any:
    return await proxy.handle(
        method="POST",
        path=path,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode(),
    )


# --------------------------------------------------------------------------


async def test_openai_batch_round_trip_and_headers() -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    result = await _post_json(
        proxy, "v1/chat/completions", {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )

    assert result.status_code == 200
    data = json.loads(result.body)
    assert data["choices"][0]["message"]["content"] == f"mail {EMAIL}"
    assert "x-privyx-session" in result.headers
    assert result.headers.get("x-request-id") == "req-1"  # upstream header preserved


async def test_upstream_receives_pseudonym_not_pii() -> None:
    engine = _engine()
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(capture))

    await _post_json(
        proxy, "v1/chat/completions", {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )

    sent = capture[0].content.decode()
    assert EMAIL not in sent
    assert "<PRIVYX_" in sent


async def test_openai_stream_round_trip_done_last() -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        ).encode(),
    )

    assert result.stream is not None
    assert result.media_type == "text/event-stream"
    out = "".join([chunk async for chunk in result.stream])
    assert "<PRIVYX_" not in out
    assert EMAIL in out
    assert out.rstrip().endswith("data: [DONE]")


async def test_anthropic_batch_round_trip() -> None:
    engine = _engine()
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(capture))

    result = await _post_json(
        proxy,
        "v1/messages",
        {"system": f"op {EMAIL}", "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
    )

    data = json.loads(result.body)
    assert data["content"][0]["text"] == f"mail {EMAIL}"
    sent = capture[0].content.decode()
    assert EMAIL not in sent  # both system and message pseudonymized


@pytest.mark.parametrize("stream", [False, True], ids=["batch", "stream"])
async def test_a_token_id_written_on_its_own_is_restored(stream: bool) -> None:
    """A model sometimes keeps only a token's id; an anchor's is long enough to stand alone."""
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(anchor=HMACAnchor("test-secret")),
        vault=MemoryVault(),
    )
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(capture))

    async def ask(content: str) -> str:
        result = await proxy.handle(
            method="POST",
            path="v1/chat/completions",
            headers={"content-type": "application/json", "x-privyx-session": "s1"},
            body=json.dumps(
                {"stream": stream, "messages": [{"role": "user", "content": content}]}
            ).encode(),
        )
        if result.stream is None:
            return result.body.decode()
        return "".join([chunk async for chunk in result.stream])

    await ask(f"mail {EMAIL}")
    token = json.loads(capture[0].content)["messages"][0]["content"].removeprefix("mail ")
    ident = token.strip("<>").rpartition("_")[2]
    assert len(ident) == 16

    out = await ask(f"look up {ident}")  # the upstream echoes it: the id and nothing else

    assert ident not in out
    assert EMAIL in out


async def test_concurrent_requests_use_isolated_sessions() -> None:
    """Two callers must not share a pseudonym map (the veilstream single-session bug)."""
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    async def call(email: str) -> str:
        result = await _post_json(
            proxy,
            "v1/chat/completions",
            {"messages": [{"role": "user", "content": f"mail {email}"}]},
        )
        return json.loads(result.body)["choices"][0]["message"]["content"]

    first, second = await asyncio.gather(call(EMAIL), call(EMAIL2))

    assert first == f"mail {EMAIL}"
    assert second == f"mail {EMAIL2}"


async def test_a_path_that_repeats_v1_is_masked_before_the_upstream_answers_404() -> None:
    """What an Anthropic client calls when its base URL ends in ``/v1``."""
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))

    result = await _post_json(
        proxy, "v1/v1/messages", {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )

    assert result.status_code == 404
    assert capture[0].url.path == "/v1/v1/messages"
    sent = capture[0].content.decode()
    assert EMAIL not in sent
    assert "<PRIVYX_" in sent


async def test_non_chat_path_is_forwarded_verbatim() -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    result = await proxy.handle(method="GET", path="v1/models", headers={}, body=b"")

    assert result.stream is not None  # nothing to restore: relayed as it arrives
    data = json.loads(b"".join([chunk async for chunk in result.stream]))
    # Token-shaped text on an unrouted path is not transformed.
    assert data["data"][0]["id"] == "gpt-x <PRIVYX_EMAIL_9>"


async def test_unrouted_path_refused_without_passthrough() -> None:
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(
        _engine(),
        origin="https://up.test",
        client=_mock_client(capture),
        passthrough_unknown=False,
    )

    refused = await _post_json(proxy, "v1/embeddings", {"input": f"mail {EMAIL}"})
    routed = await _post_json(
        proxy, "v1/chat/completions", {"messages": [{"role": "user", "content": EMAIL}]}
    )

    assert refused.status_code == 403
    assert "passthrough_unknown" in json.loads(refused.body)["error"]["message"]
    assert routed.status_code == 200
    assert [r.url.path for r in capture] == ["/v1/chat/completions"]  # nothing leaked


async def test_websocket_upgrade_is_refused_with_426() -> None:
    """A WebSocket could not be relayed, so its frames could not be masked.

    Codex opens ``/v1/responses`` as one first.  ``426`` makes it fall back to
    HTTP at once; on another status it retries for seconds before it does.
    """
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))

    refused = await proxy.handle(
        method="GET",
        path="v1/responses",
        headers={"Connection": "Upgrade", "Upgrade": "websocket"},
        body=b"",
    )

    assert refused.status_code == 426
    assert "WebSocket" in json.loads(refused.body or b"{}")["error"]["message"]
    assert capture == []  # not forwarded as a plain GET


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", f"mail {EMAIL}"),
        ("application/json", json.dumps([{"role": "user", "content": EMAIL}])),
        ("application/x-www-form-urlencoded", f"mail={EMAIL}"),
    ],
    ids=["not-json", "array", "form"],
)
async def test_a_routed_body_that_is_not_a_json_object_is_never_forwarded(
    content_type: str, body: str
) -> None:
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": content_type},
        body=body.encode(),
    )

    assert result.status_code == 400
    assert json.loads(result.body)["error"]["type"] == "privyx_invalid_request"
    assert not capture


async def test_a_json_body_is_masked_whatever_its_content_type() -> None:
    # `curl -d` sends application/x-www-form-urlencoded unless told otherwise.
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=json.dumps({"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}).encode(),
    )

    assert result.status_code == 200
    assert EMAIL not in capture[-1].content.decode()


def _codex_metadata() -> str:
    """Codex's turn metadata: JSON with the workspace path as a key."""
    return json.dumps({"turn_id": "t-1", "workspaces": {f"/home/{EMAIL}/app": {}}})


async def test_codex_turn_metadata_header_is_masked_with_the_body() -> None:
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))
    metadata = _codex_metadata()
    payload = {"input": f"mail {EMAIL}", "client_metadata": {"x-codex-turn-metadata": metadata}}

    await proxy.handle(
        method="POST",
        path="v1/responses",
        headers={"content-type": "application/json", "X-Codex-Turn-Metadata": metadata},
        body=json.dumps(payload).encode(),
    )

    sent = capture[0]
    body = json.loads(sent.content)
    token = body["input"].removeprefix("mail ")
    assert token.startswith("<PRIVYX_EMAIL_")
    # One value, one token: in the input, the body's copy, and the header.
    assert sent.headers["x-codex-turn-metadata"] == body["client_metadata"]["x-codex-turn-metadata"]
    assert list(json.loads(sent.headers["x-codex-turn-metadata"])["workspaces"]) == [
        f"/home/{token}/app"
    ]


@pytest.mark.parametrize("path", ["v1/responses", "v1/models"], ids=["routed", "unrouted"])
async def test_codex_turn_metadata_header_is_masked_without_a_body(path: str) -> None:
    # A request without a body carries the header too.
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=_mock_client(capture))

    await proxy.handle(
        method="GET", path=path, headers={"x-codex-turn-metadata": _codex_metadata()}, body=b""
    )

    sent = capture[0].headers["x-codex-turn-metadata"]
    assert EMAIL not in sent
    assert list(json.loads(sent)["workspaces"])[0].startswith("/home/<PRIVYX_EMAIL_")
    assert capture[0].content == b""


class _BrokenDetector:
    name = "broken"

    async def detect(self, text: str, context: Any) -> Any:
        raise DetectorError("boom")


async def test_a_header_that_cannot_be_masked_is_never_forwarded() -> None:
    capture: list[httpx.Request] = []
    engine = PrivacyEngine(
        detector=_BrokenDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(capture))

    result = await proxy.handle(
        method="GET",
        path="v1/responses",
        headers={"x-codex-turn-metadata": _codex_metadata()},
        body=b"",
    )

    assert result.status_code == 503
    assert json.loads(result.body)["error"]["type"] == "privyx_scan_failed"
    assert not capture


def _audited_proxy(buf: io.StringIO, client: httpx.AsyncClient) -> TransparentProxy:
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    return TransparentProxy(engine, origin="https://up.test", client=client, audit=audit)


async def test_audit_trail_records_request_without_pii() -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    # Two PII-bearing message leaves in one request: the per-leaf transforms must
    # aggregate into a single session.transform line.
    await _post_json(
        proxy,
        "v1/chat/completions",
        {
            "messages": [
                {"role": "user", "content": f"mail {EMAIL}"},
                {"role": "user", "content": f"cc {EMAIL2}"},
            ]
        },
    )

    written = buf.getvalue()
    assert EMAIL not in written and EMAIL2 not in written  # never any payload content

    records = [json.loads(line) for line in written.splitlines() if line]

    transforms = [r for r in records if r["event"] == "session.transform"]
    assert len(transforms) == 1  # aggregated, not one line per leaf
    assert transforms[0]["entity_counts"] == {"EMAIL": 2}
    assert transforms[0]["transformations"] == 2

    request = next(r for r in records if r["event"] == "proxy.request")
    assert request["method"] == "POST"
    assert request["path"] == "/v1/chat/completions"
    assert request["schema"] == "openai"
    assert request["status"] == 200
    assert request["stream"] is False
    assert request["upstream"] == "up.test"
    assert isinstance(request["duration_ms"], int | float)
    assert 0 <= request["transform_ms"] <= request["duration_ms"]

    response = next(r for r in records if r["event"] == "proxy.response")
    assert response["stream"] is False
    assert response["bytes"] > 0
    assert response["restored"] >= 1

    # Every event of the exchange shares one non-null request_id.
    request_ids = {r["request_id"] for r in records}
    assert request_ids == {request["request_id"]}
    assert request["request_id"] is not None


async def test_transform_ms_is_left_out_when_nothing_was_masked() -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    await proxy.handle(method="GET", path="v1/models", headers={}, body=b"")

    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    request = next(r for r in records if r["event"] == "proxy.request")
    assert "transform_ms" not in request


async def test_streaming_audit_records_restore_and_response() -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        ).encode(),
    )
    # The response/restore events are emitted only once the stream is fully drained.
    assert result.stream is not None
    _ = "".join([chunk async for chunk in result.stream])

    written = buf.getvalue()
    assert EMAIL not in written

    records = [json.loads(line) for line in written.splitlines() if line]
    restore = next(r for r in records if r["event"] == "session.restore")
    assert restore["transformations"] >= 1

    response = next(r for r in records if r["event"] == "proxy.response")
    assert response["stream"] is True
    assert response["frames"] > 0
    assert response["restored"] >= 1

    # request_id ties the streamed response back to its request.
    request = next(r for r in records if r["event"] == "proxy.request")
    assert response["request_id"] == request["request_id"]
    assert restore["request_id"] == request["request_id"]


async def test_ephemeral_batch_session_is_deleted() -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    result = await _post_json(
        proxy,
        "v1/chat/completions",
        {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
    )
    sid = result.headers["x-privyx-session"]

    assert await proxy._engine.vault.get(sid) is None  # noqa: SLF001
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    deleted = next(r for r in records if r["event"] == "session.deleted")
    assert deleted["session_id"] == sid
    assert deleted["reason"] == "ephemeral_request_complete"
    assert deleted["mapping_count"] == 1


async def test_ephemeral_stream_session_is_deleted_after_drain() -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        ).encode(),
    )
    sid = result.headers["x-privyx-session"]
    assert result.stream is not None
    _ = "".join([chunk async for chunk in result.stream])

    assert await proxy._engine.vault.get(sid) is None  # noqa: SLF001
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    assert any(r["event"] == "session.deleted" and r["session_id"] == sid for r in records)


@pytest.mark.parametrize("pulled", [0, 1], ids=["before-first-chunk", "after-first-chunk"])
async def test_ephemeral_stream_session_is_deleted_when_closed_early(pulled: int) -> None:
    buf = io.StringIO()
    proxy = _audited_proxy(buf, _mock_client())

    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        ).encode(),
    )
    sid = result.headers["x-privyx-session"]
    assert result.stream is not None
    for _ in range(pulled):
        await anext(result.stream)
    await result.stream.aclose()

    assert await proxy._engine.vault.get(sid) is None  # noqa: SLF001
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    deleted = [r for r in records if r["event"] == "session.deleted"]
    assert [r["session_id"] for r in deleted] == [sid]  # once, however it was closed


async def test_sticky_session_is_retained_and_not_deleted() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(), audit=audit)

    _ = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json", "x-privyx-session": "ses_sticky_1"},
        body=json.dumps({"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}).encode(),
    )
    second = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json", "x-privyx-session": "ses_sticky_1"},
        body=json.dumps({"messages": [{"role": "user", "content": f"again {EMAIL}"}]}).encode(),
    )

    assert second.headers["x-privyx-session"] == "ses_sticky_1"
    assert await engine.vault.get("ses_sticky_1") is not None
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    assert not any(r["event"] == "session.deleted" for r in records)


async def test_upstream_failure_records_proxy_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {EMAIL}")  # message carries PII

    buf = io.StringIO()
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    proxy = _audited_proxy(buf, client)

    result = await _post_json(
        proxy,
        "v1/chat/completions",
        {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
    )

    assert result.status_code == 502
    assert EMAIL not in result.body.decode()  # the class is named, never the message
    written = buf.getvalue()
    assert EMAIL not in written  # the exception message must not leak into the trail

    records = [json.loads(line) for line in written.splitlines() if line]
    error = next(r for r in records if r["event"] == "proxy.error")
    assert error["phase"] == "upstream"
    assert error["error_type"] == "ConnectError"
    assert error["request_id"] is not None


@pytest.mark.parametrize(
    ("failure", "status", "kind"),
    [
        (httpx.ConnectError, 502, "unreachable"),
        (httpx.RemoteProtocolError, 502, "unreachable"),
        (httpx.ConnectTimeout, 504, "timeout"),
        (httpx.ReadTimeout, 504, "timeout"),
        (httpx.PoolTimeout, 503, "busy"),
    ],
)
async def test_no_upstream_response_is_named_by_status_and_type(
    failure: type[Exception], status: int, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure("")  # a timeout's message is empty

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    proxy = TransparentProxy(_engine(), origin="https://up.test", client=client)

    result = await _post_json(
        proxy, "v1/chat/completions", {"messages": [{"role": "user", "content": EMAIL}]}
    )

    error = json.loads(result.body)["error"]
    assert result.status_code == status
    assert result.media_type == "application/json"
    assert error["type"] == f"privyx_upstream_{kind}"
    assert error["message"].endswith(f"({failure.__name__})")
    assert result.headers.get("retry-after") == ("10" if status == 503 else None)
    # Expected conditions: one WARNING line, the traceback only at DEBUG.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]


async def test_session_header_reused_across_requests() -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    first = await _post_json(
        proxy, "v1/chat/completions", {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )
    sid = first.headers["x-privyx-session"]

    reused = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"content-type": "application/json", "x-privyx-session": sid},
        body=json.dumps({"messages": [{"role": "user", "content": f"again {EMAIL}"}]}).encode(),
    )

    assert reused.headers["x-privyx-session"] == sid


# -- session strategies (header-less clients) -------------------------------


def _auth_headers() -> dict[str, str]:
    return {"content-type": "application/json", "authorization": "Bearer sk-test"}


async def _turn(proxy: TransparentProxy, messages: list[dict[str, Any]]) -> Any:
    """POST a chat turn with a credential but no x-privyx-session header."""
    return await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers=_auth_headers(),
        body=json.dumps({"messages": messages}).encode(),
    )


async def test_conversation_strategy_reuses_one_session_across_turns() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    proxy = TransparentProxy(
        engine,
        origin="https://up.test",
        client=_mock_client(),
        audit=audit,
        session_strategy="conversation",
    )

    first = await _turn(proxy, [{"role": "user", "content": f"mail {EMAIL}"}])
    second = await _turn(
        proxy,
        [
            {"role": "user", "content": f"mail {EMAIL}"},  # same opening line
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and one more thing"},
        ],
    )

    # Same conversation → one reused session across both turns.
    assert first.headers["x-privyx-session"] == second.headers["x-privyx-session"]

    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    created = [r for r in records if r["event"] == "session.created"]
    assert len(created) == 1
    assert created[0]["source"] == "conversation"


async def test_conversation_strategy_isolates_distinct_conversations() -> None:
    engine = _engine()
    proxy = TransparentProxy(
        engine,
        origin="https://up.test",
        client=_mock_client(),
        session_strategy="conversation",
    )

    a = await _turn(proxy, [{"role": "user", "content": "topic one"}])
    b = await _turn(proxy, [{"role": "user", "content": "topic two"}])

    assert a.headers["x-privyx-session"] != b.headers["x-privyx-session"]


async def test_default_strategy_is_ephemeral_and_audited() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client(), audit=audit)

    a = await _turn(proxy, [{"role": "user", "content": f"mail {EMAIL}"}])
    b = await _turn(proxy, [{"role": "user", "content": f"mail {EMAIL}"}])

    # No continuity by default: a fresh session per request.
    assert a.headers["x-privyx-session"] != b.headers["x-privyx-session"]
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    created = [r for r in records if r["event"] == "session.created"]
    assert len(created) == 2
    assert {r["source"] for r in created} == {"ephemeral"}
