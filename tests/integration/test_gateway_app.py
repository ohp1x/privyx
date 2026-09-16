"""End-to-end tests for the gateway app (``--gateway``) through its ASGI app.

The gateway exposes *its own* endpoints and posts every one of them to the
single upstream endpoint from ``provider.base_url``, used verbatim — the
property that lets an upstream carry a base path.  These tests pin both halves:
the route map comes from ``proxy.routes`` (so ``/v1/messages`` is served, not
just ``/v1/chat/completions``), and the wire schema used to restore a response
comes from the route.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from privyx.config.schema import Settings  # noqa: E402
from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.gateway.server import Gateway  # noqa: E402
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


def _client(engine: PrivacyEngine) -> tuple[httpx.AsyncClient, Any]:
    provider = GenericProvider(
        base_url=UPSTREAM,
        headers={"x-api-key": "sk-test"},
        client=_upstream_client(),
    )
    settings = Settings(provider={"type": "anthropic", "base_url": UPSTREAM})
    gateway = Gateway(engine=engine, provider=provider, settings=settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ), gateway


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
async def test_routes_come_from_config() -> None:
    """Both configured paths are registered; unknown ones still 404."""
    client, gateway = _client(_engine())
    assert gateway.routes == {
        "/v1/chat/completions": "openai",
        "/v1/messages": "anthropic",
    }
    async with client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.post("/v1/responses", json={})).status_code == 404
