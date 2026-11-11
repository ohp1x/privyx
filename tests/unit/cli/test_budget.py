"""The per-launch budget of ``privyx run``."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from privyx.cli.budget import Budget
from privyx.config.schema import BudgetConfig, ProxyConfig
from privyx.observability.audit import AuditLogger
from privyx.observability.usage import merge_usage, usage_tokens

ROUTES = ProxyConfig().routes

ANTHROPIC_SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":10,"cache_read_input_tokens":90,"output_tokens":1}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
)


def _upstream(body: bytes, content_type: bytes, chunk: int = 7) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", content_type)],
            }
        )
        for at in range(0, len(body), chunk):  # split anywhere, as a stream may be
            await send(
                {"type": "http.response.body", "body": body[at : at + chunk], "more_body": True}
            )
        await send({"type": "http.response.body", "body": b""})

    return app


async def _call(app: Any, path: str, body: bytes = b"") -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body}

    headers = [(b"content-length", str(len(body)).encode())]
    await app({"type": "http", "path": path, "headers": headers}, receive, send)
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


@pytest.mark.parametrize(
    ("events", "tokens"),
    [
        ([{"usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}], 12),
        ([{"response": {"usage": {"input_tokens": 3, "output_tokens": 4}}}], 7),
        (
            [
                {"message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
                {"usage": {"output_tokens": 25}},  # a running count, not an increment
            ],
            35,
        ),
        ([{"usage": {"input_tokens": True}}, None, "x"], 0),
    ],
)
def test_usage_is_read_from_every_reply_shape(events: list[Any], tokens: int) -> None:
    fields: dict[str, int] = {}
    for event in events:
        merge_usage(fields, event)
    assert usage_tokens(fields) == tokens


async def test_turn_budget_refuses_the_request_after_the_limit() -> None:
    buf = io.StringIO()
    reply = _upstream(b'{"usage": {"total_tokens": 3}}', b"application/json")
    app = Budget(reply, BudgetConfig(max_turns=2), ROUTES, AuditLogger(buf), "claude")

    statuses = [(await _call(app, "/v1/messages"))[0] for _ in range(4)]
    free = await _call(app, "/v1/messages/count_tokens")
    other = await _call(app, "/v1/models")

    assert statuses == [200, 200, 400, 400]
    assert (free[0], other[0]) == (200, 200)
    assert (app.turns, app.tokens, app.exceeded) == (2, 6, "turn budget (2 turns) is reached")
    error = json.loads((await _call(app, "/v1/chat/completions"))[1])["error"]
    assert error["type"] == "privyx_budget_exceeded"
    assert "'claude'" in error["message"]
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [(e["event"], e["limit"], e["turns"]) for e in events] == [
        ("run.budget_exceeded", "turn", 2)
    ]


async def test_token_budget_counts_a_stream_split_anywhere() -> None:
    reply = _upstream(ANTHROPIC_SSE, b"text/event-stream; charset=utf-8")
    app = Budget(reply, BudgetConfig(max_tokens=200), ROUTES, AuditLogger(None), "claude")

    statuses = [(await _call(app, "/v1/messages"))[0] for _ in range(3)]

    assert statuses == [200, 200, 400]  # 125 tokens each: the second crosses 200
    assert app.tokens == 250
    assert app.exceeded == "token budget (200 tokens) is reached"


GEMINI_SSE = (
    b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":40,"totalTokenCount":40}}\r\n\r\n'
    b'data: {"candidates":[],"usageMetadata":{"promptTokenCount":40,"candidatesTokenCount":20,'
    b'"thoughtsTokenCount":10,"totalTokenCount":70}}\r\n\r\n'
)
GEMINI_ARRAY = b'[{"candidates":[]},{"usageMetadata":{"totalTokenCount":70}}]'
OLLAMA_LINES = (
    b'{"model":"llama3.2","message":{"content":"hi"},"done":false}\n'
    b'{"model":"llama3.2","message":{"content":""},"done":true,'
    b'"prompt_eval_count":50,"eval_count":20}\n'
)


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        ("/v1beta/models/gemini-2.5-flash:streamGenerateContent", GEMINI_SSE, b"text/event-stream"),
        (
            "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
            GEMINI_ARRAY,
            b"application/json",
        ),
        ("/api/chat", OLLAMA_LINES, b"application/x-ndjson"),
    ],
    ids=["gemini-sse", "gemini-array", "ollama"],
)
async def test_gemini_and_ollama_turns_are_counted(
    path: str, body: bytes, content_type: bytes
) -> None:
    app = Budget(
        _upstream(body, content_type), BudgetConfig(max_tokens=100), ROUTES, AuditLogger(None), "x"
    )

    assert [(await _call(app, path))[0] for _ in range(3)] == [200, 200, 400]
    assert (app.turns, app.tokens, app.unmeasured) == (2, 140, False)
    for free in (":countTokens", ":embedContent", ":batchEmbedContents"):
        assert (await _call(app, f"/v1beta/models/m{free}"))[0] == 200  # not a turn, not refused


CHAT_SSE_WITH_USAGE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
    b"data: [DONE]\n\n"
)


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
    received: list[tuple[bytes, dict[bytes, bytes]]] = []
    reply = _upstream(CHAT_SSE_WITH_USAGE, b"text/event-stream")

    async def upstream(scope: Any, receive: Any, send: Any) -> None:
        received.append(((await receive())["body"], dict(scope["headers"])))
        await reply(scope, receive, send)

    app = Budget(upstream, BudgetConfig(max_tokens=100), ROUTES, AuditLogger(None), "x")

    await _call(app, "/v1/chat/completions", json.dumps(request_body).encode())

    body, headers = received[0]
    assert json.loads(body).get("stream_options") == options
    assert int(headers[b"content-length"]) == len(body)
    assert app.tokens == 5


async def test_a_reply_without_usage_stops_a_token_budget_instead_of_counting_zero() -> None:
    buf = io.StringIO()
    reply = _upstream(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"text/event-stream")
    app = Budget(reply, BudgetConfig(max_tokens=100), ROUTES, AuditLogger(buf), "x")
    request = json.dumps(
        {"stream": True, "messages": [], "stream_options": {"include_usage": False}}
    ).encode()

    statuses = [(await _call(app, "/v1/chat/completions", request))[0] for _ in range(2)]

    assert statuses == [200, 400]
    assert app.exceeded == "token budget cannot be measured: a reply came without usage"
    assert json.loads(buf.getvalue())["limit"] == "unmeasured"


async def test_a_cancelled_stream_or_a_turn_budget_alone_is_not_unmeasured() -> None:
    async def cancelled(scope: Any, receive: Any, send: Any) -> None:
        start = {"type": "http.response.start", "status": 200, "headers": []}
        await send(start)
        await send({"type": "http.response.body", "body": b"data: {}\n", "more_body": True})

    no_usage = _upstream(b'{"choices": []}', b"application/json")
    token_app = Budget(cancelled, BudgetConfig(max_tokens=100), ROUTES, AuditLogger(None), "x")
    turn_app = Budget(no_usage, BudgetConfig(max_turns=5), ROUTES, AuditLogger(None), "x")

    assert [(await _call(token_app, "/v1/messages"))[0] for _ in range(2)] == [200, 200]
    assert [(await _call(turn_app, "/v1/messages"))[0] for _ in range(2)] == [200, 200]


async def test_run_stops_the_tool_at_its_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    upstream: tuple[str, Any],
) -> None:
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.delenv("PRIVYX_UPSTREAM_URL", raising=False)
    url, seen = upstream

    from privyx.cli.commands.run import Target, _run_target

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.request, urllib.error\n"
        "for _ in range(3):\n"
        "    try:\n"
        "        urllib.request.urlopen(urllib.request.Request(\n"
        "            os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',\n"
        "            data=b'{\"messages\": []}',\n"
        "            headers={'content-type': 'application/json'},\n"
        "        ), timeout=5).read()\n"
        "    except urllib.error.HTTPError as exc:\n"
        "        print(exc.code)\n",
        encoding="utf-8",
    )
    spec = Target(
        command=sys.executable,
        provider="anthropic",
        env_vars=("ANTHROPIC_BASE_URL",),
        budget=BudgetConfig(max_turns=2),
    )
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=None,
        upstream=url,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0
    assert len(seen) == 2
    assert "the turn budget (2 turns) is reached, after 2 turn(s)" in capsys.readouterr().err
    assert '"event": "run.budget_exceeded"' in (tmp_path / "audit.log").read_text()
