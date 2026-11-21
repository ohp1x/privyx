"""``request.received`` and ``request.masked``: what a listener changes, and how it ends one."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import httpx

from privyx.core import EventBus, Exchange
from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.events import ProxyResponse, RequestMasked, RequestReceived
from privyx.observability.audit import AuditLogger
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.proxy import Pipeline
from privyx.vaults import MemoryVault

EMAIL = "alice@example.com"
CHAT = {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}


class _Proxy:
    """A pipeline whose upstream echoes the last message, and what it saw."""

    def __init__(self, *listeners: tuple[type[Any], Callable[[Any], Any]]) -> None:
        self.sent: list[httpx.Request] = []
        self.trail = io.StringIO()
        bus = EventBus()
        for event, listener in listeners:
            bus.add(event, listener)
        audit = AuditLogger(self.trail, events=bus)
        engine = PrivacyEngine(
            detector=RegexDetector(),
            policy=DefaultPolicy(),
            operator=PseudonymOperator(),
            vault=MemoryVault(),
            audit=audit,
            dlp_check=True,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._upstream))
        self.pipeline = Pipeline(
            engine, origin="https://up.test", client=client, audit=audit, events=bus
        )

    def _upstream(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        text = json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    async def post(self, body: dict[str, Any], **headers: str) -> tuple[int, Any]:
        exchange = Exchange(
            method="POST",
            path="/v1/chat/completions",
            headers={"content-type": "application/json", **headers},
            body=json.dumps(body).encode(),
        )
        reply = await self.pipeline.handle(exchange)
        return reply.status_code, json.loads(reply.body or b"null")

    def events(self, name: str) -> list[dict[str, Any]]:
        records = [json.loads(line) for line in self.trail.getvalue().splitlines()]
        return [r for r in records if r["event"] == name]


async def test_what_a_received_listener_adds_is_masked_and_sent() -> None:
    def add(event: RequestReceived) -> None:
        event.body["messages"].insert(0, {"role": "system", "content": f"cc {EMAIL}"})
        event.headers["x-team"] = "apollo"

    proxy = _Proxy((RequestReceived, add))

    status, _ = await proxy.post(CHAT)

    [sent] = proxy.sent
    assert status == 200
    assert EMAIL not in sent.content.decode()
    assert json.loads(sent.content)["messages"][0]["content"].startswith("cc <PRIVYX_EMAIL_")
    assert sent.headers["x-team"] == "apollo"


async def test_a_masked_listener_sees_the_masked_request_and_its_change_is_sent() -> None:
    seen: list[str] = []

    def tag(event: RequestMasked) -> None:
        seen.append(json.dumps(event.body))
        event.body["metadata"] = {"team": "apollo"}
        event.headers["x-team"] = "apollo"

    proxy = _Proxy((RequestMasked, tag))

    status, reply = await proxy.post(CHAT)

    assert status == 200
    assert EMAIL not in seen[0] and "<PRIVYX_EMAIL_" in seen[0]
    [sent] = proxy.sent
    assert json.loads(sent.content)["metadata"] == {"team": "apollo"}
    assert sent.headers["x-team"] == "apollo"
    assert reply["choices"][0]["message"]["content"] == f"mail {EMAIL}"  # restored


async def test_a_masked_value_a_masked_listener_writes_back_is_refused() -> None:
    def leak(event: RequestMasked) -> None:
        event.headers["x-note"] = EMAIL

    proxy = _Proxy((RequestMasked, leak))

    status, reply = await proxy.post(CHAT)

    assert (status, reply["error"]["type"]) == (422, "privyx_leak_blocked")
    assert proxy.sent == []


async def test_reject_answers_with_an_error_and_sends_nothing() -> None:
    def refuse(event: RequestReceived) -> None:
        event.reject(403, "acme_policy", "this model is not allowed")

    proxy = _Proxy((RequestReceived, refuse))

    status, reply = await proxy.post(CHAT)

    assert (status, reply) == (
        403,
        {"error": {"type": "acme_policy", "message": "this model is not allowed"}},
    )
    assert proxy.sent == []


async def test_a_received_answer_is_sent_as_it_is() -> None:
    def answer(event: RequestReceived) -> None:
        event.respond(200, {"cached": "<PRIVYX_EMAIL_1>"})

    proxy = _Proxy((RequestReceived, answer))

    assert await proxy.post(CHAT) == (200, {"cached": "<PRIVYX_EMAIL_1>"})
    assert proxy.sent == []


async def test_a_masked_answer_is_restored_like_an_upstream_reply() -> None:
    def answer(event: RequestMasked) -> None:
        event.respond(200, {"choices": [{"message": event.body["messages"][-1]}]})

    proxy = _Proxy((RequestMasked, answer))

    status, reply = await proxy.post(CHAT)

    assert status == 200
    assert reply["choices"][0]["message"]["content"] == f"mail {EMAIL}"
    assert proxy.sent == []


async def test_a_listener_cannot_write_a_credential_header() -> None:
    def swap(event: RequestReceived) -> None:
        event.headers["Authorization"] = "Bearer " + "sk-" + "other"

    proxy = _Proxy((RequestReceived, swap))

    status, reply = await proxy.post(CHAT, authorization="Bearer " + "sk-" + "mine")

    assert (status, reply["error"]["type"]) == (500, "privyx_listener_failed")
    assert proxy.sent == []
    [error] = proxy.events("proxy.error")
    assert (error["phase"], error["error_type"]) == ("listener", "PermissionError")
    assert "swap" in error["listener"]


async def test_a_failing_listener_refuses_the_exchange_without_its_message() -> None:
    def broken(event: RequestMasked) -> None:
        raise ValueError(f"cannot handle {event.body}")

    proxy = _Proxy((RequestMasked, broken))

    status, reply = await proxy.post(CHAT)

    assert (status, reply["error"]["type"]) == (500, "privyx_listener_failed")
    assert proxy.sent == []
    assert "PRIVYX_EMAIL" not in json.dumps(reply) + proxy.trail.getvalue()


async def test_a_failing_notification_listener_leaves_the_exchange_alone() -> None:
    def broken(event: ProxyResponse) -> None:
        raise RuntimeError("boom")

    proxy = _Proxy((ProxyResponse, broken))

    status, _ = await proxy.post(CHAT)

    assert status == 200
    assert len(proxy.sent) == 1
