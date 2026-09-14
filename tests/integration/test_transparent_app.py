"""End-to-end tests for the transparent proxy through its ASGI app.

These drive :func:`create_transparent_app` with an in-process ASGI transport, so
the FastAPI catch-all route, ``/health``, and the ``ProxyResponse`` →
``Response``/``StreamingResponse`` conversion are all exercised — the layer the
direct ``TransparentProxy.handle`` tests skip.  The upstream is still a mock.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.gateway.transparent import create_transparent_app  # noqa: E402
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


def _upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
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


def _app() -> Any:
    engine = _engine()
    proxy = TransparentProxy(engine, origin="https://up.test", client=_upstream())
    return create_transparent_app(engine, proxy)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test")


async def test_health() -> None:
    async with _client(_app()) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
