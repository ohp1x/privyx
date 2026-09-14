"""Integration tests for the transparent (drop-in) reverse proxy.

The upstream is an :class:`httpx.MockTransport` that echoes the request's last
user message back as the assistant reply, so a round-trip that returns the
original PII proves the request was pseudonymized upstream and the response
restored on the way back.  A capture list lets tests assert what the upstream
actually received.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from privyx.core.engine import PrivacyEngine
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
                frames = "".join(
                    f"data: {json.dumps({'choices': [{'delta': {'content': part}}]})}\n\n"
                    for part in _chunks(content)
                ) + "data: [DONE]\n\n"
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


async def _post_json(
    proxy: TransparentProxy, path: str, payload: dict[str, Any]
) -> Any:
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


async def test_non_chat_path_is_forwarded_verbatim() -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_mock_client())

    result = await proxy.handle(method="GET", path="v1/models", headers={}, body=b"")

    data = json.loads(result.body)
    # Token-shaped text on an unrouted path is not transformed.
    assert data["data"][0]["id"] == "gpt-x <PRIVYX_EMAIL_9>"


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
