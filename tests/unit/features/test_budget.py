"""The ``budget`` feature: a launch's model turns refused once a limit is reached."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from privyx.core import EventBus, Exchange, listened
from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.events import RunBudgetExceeded
from privyx.features import Budget
from privyx.observability.audit import AuditLogger
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.proxy import Pipeline
from privyx.testing import services
from privyx.vaults import MemoryVault

Upstream = Callable[[httpx.Request], httpx.Response]

ANTHROPIC_SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":10,"cache_read_input_tokens":90,"output_tokens":1}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
)
GEMINI_SSE = (
    b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":40,"totalTokenCount":40}}\r\n\r\n'
    b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":20,'
    b'"thoughtsTokenCount":10,"totalTokenCount":70}}\r\n\r\n'
)
GEMINI_ARRAY = (
    b'[{"candidates":[]},{"usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":30,'
    b'"totalTokenCount":70}}]'
)
OLLAMA_LINES = (
    b'{"model":"llama3.2","message":{"content":"hi"},"done":false}\n'
    b'{"model":"llama3.2","message":{"content":""},"done":true,'
    b'"prompt_eval_count":50,"eval_count":20}\n'
)
CHAT_SSE_WITH_USAGE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
    b"data: [DONE]\n\n"
)


def _reply(body: bytes, content_type: str, seen: list[httpx.Request] | None = None) -> Upstream:
    def reply(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    return reply


async def _proxy(
    upstream: Upstream, **options: Any
) -> tuple[Pipeline, Budget, list[RunBudgetExceeded]]:
    """A pipeline to ``upstream`` with the budget on, and the notifications of its stop."""
    bus = EventBus()
    budget = Budget(profile="claude", **options)
    await budget.open(services(events=bus))
    for event, priority, listener in listened(budget):
        bus.add(event, listener, priority=priority)
    stopped: list[RunBudgetExceeded] = []
    bus.add(RunBudgetExceeded, stopped.append)
    audit = AuditLogger(events=bus)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy = Pipeline(engine, origin="https://up.test", client=client, audit=audit, events=bus)
    return proxy, budget, stopped


async def _send(
    proxy: Pipeline, path: str, body: dict[str, Any] | None = None, *, read: bool = True
) -> tuple[int, bytes]:
    """One exchange: its status, and its reply, a stream read to its end unless not ``read``."""
    raw = json.dumps({"messages": []} if body is None else body).encode()
    headers = {"content-type": "application/json"}
    reply = await proxy.handle(Exchange(method="POST", path=path, headers=headers, body=raw))
    if reply.stream is None:
        return reply.status_code, reply.body or b""
    chunks: list[bytes] = []
    async for chunk in reply.stream:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if not read:
            await reply.stream.aclose()  # type: ignore[attr-defined]
            break
    return reply.status_code, b"".join(chunks)


async def test_turn_budget_refuses_the_request_after_the_limit() -> None:
    proxy, budget, stopped = await _proxy(
        _reply(b'{"usage": {"input_tokens": 1, "output_tokens": 2}}', "application/json"),
        max_turns=2,
    )

    statuses = [(await _send(proxy, "/v1/messages"))[0] for _ in range(4)]
    free = await _send(proxy, "/v1/messages/count_tokens")

    assert statuses == [200, 200, 400, 400]
    assert free[0] == 200
    assert (budget.turns, budget.tokens, budget.exceeded) == (
        2,
        6,
        "turn budget (2 turns) is reached",
    )
    status, body = await _send(proxy, "/v1/chat/completions")
    error = json.loads(body)["error"]
    assert (status, error["type"]) == (400, "privyx_budget_exceeded")
    assert "'claude'" in error["message"]
    assert [(e.fields["limit"], e.fields["turns"]) for e in stopped] == [("turn", 2)]
    assert stopped[0].to_dict()["event"] == "run.budget_exceeded"


async def test_token_budget_counts_a_streamed_reply() -> None:
    proxy, budget, _ = await _proxy(
        _reply(ANTHROPIC_SSE, "text/event-stream; charset=utf-8"), max_tokens=200
    )

    statuses = [(await _send(proxy, "/v1/messages", {"stream": True}))[0] for _ in range(3)]

    assert statuses == [200, 200, 400]  # 125 tokens each: the second crosses 200
    assert budget.tokens == 250
    assert budget.exceeded == "token budget (200 tokens) is reached"


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        ("/v1beta/models/gemini-2.5-flash:streamGenerateContent", GEMINI_SSE, "text/event-stream"),
        ("/v1beta/models/gemini-2.5-flash:streamGenerateContent", GEMINI_ARRAY, "application/json"),
        ("/api/chat", OLLAMA_LINES, "application/x-ndjson"),
    ],
    ids=["gemini-sse", "gemini-array", "ollama"],
)
async def test_gemini_and_ollama_turns_are_counted(
    path: str, body: bytes, content_type: str
) -> None:
    proxy, budget, _ = await _proxy(_reply(body, content_type), max_tokens=100)
    request = {"contents": []} if "gemini" in path else {"messages": []}

    assert [(await _send(proxy, path, request))[0] for _ in range(3)] == [200, 200, 400]
    assert (budget.turns, budget.tokens, budget.unmeasured) == (2, 140, False)
    for free in (":countTokens", ":embedContent", ":batchEmbedContents"):
        status, _ = await _send(proxy, f"/v1beta/models/m{free}", {"contents": []})
        assert status == 200  # not a turn, not refused


@pytest.mark.parametrize(
    ("request_body", "options"),
    [
        ({"stream": True, "messages": []}, {"include_usage": True}),
        (
            {"stream": True, "messages": [], "stream_options": {"x": 1}},
            {"x": 1, "include_usage": True},
        ),
        # The client's own choice is kept.
        (
            {"stream": True, "messages": [], "stream_options": {"include_usage": False}},
            {"include_usage": False},
        ),
        ({"messages": []}, None),  # a batch reply carries usage anyway
    ],
)
async def test_a_token_budget_asks_a_chat_stream_for_usage(
    request_body: dict[str, Any], options: Any
) -> None:
    seen: list[httpx.Request] = []
    proxy, budget, _ = await _proxy(
        _reply(CHAT_SSE_WITH_USAGE, "text/event-stream", seen), max_tokens=100
    )

    await _send(proxy, "/v1/chat/completions", request_body)

    assert json.loads(seen[0].content).get("stream_options") == options
    assert int(seen[0].headers["content-length"]) == len(seen[0].content)
    assert budget.tokens == 5


async def test_a_turn_budget_alone_does_not_ask_for_usage() -> None:
    seen: list[httpx.Request] = []
    proxy, _, _ = await _proxy(_reply(CHAT_SSE_WITH_USAGE, "text/event-stream", seen), max_turns=5)

    await _send(proxy, "/v1/chat/completions", {"stream": True, "messages": []})

    assert "stream_options" not in json.loads(seen[0].content)


async def test_a_reply_without_usage_stops_a_token_budget_instead_of_counting_zero() -> None:
    proxy, budget, stopped = await _proxy(
        _reply(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', "text/event-stream"),
        max_tokens=100,
    )
    request = {"stream": True, "messages": [], "stream_options": {"include_usage": False}}

    statuses = [(await _send(proxy, "/v1/chat/completions", request))[0] for _ in range(2)]

    assert statuses == [200, 400]
    assert budget.exceeded == "token budget cannot be measured: a reply came without usage"
    assert [e.fields["limit"] for e in stopped] == ["unmeasured"]


async def test_a_stream_the_client_left_or_a_turn_budget_alone_is_not_unmeasured() -> None:
    no_usage = _reply(
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n' * 3, "text/event-stream"
    )
    token, token_budget, _ = await _proxy(no_usage, max_tokens=100)
    turn, _, _ = await _proxy(_reply(b'{"choices": []}', "application/json"), max_turns=5)
    request = {"stream": True, "messages": [], "stream_options": {"include_usage": False}}

    left = [(await _send(token, "/v1/chat/completions", request, read=False))[0] for _ in range(2)]
    whole = [(await _send(turn, "/v1/chat/completions"))[0] for _ in range(2)]

    assert (left, whole) == ([200, 200], [200, 200])
    assert not token_budget.unmeasured


def test_a_limit_must_be_positive() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Budget(max_turns=0)
