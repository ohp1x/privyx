"""Claude Code, and a scripted Anthropic API that plays one agent loop for it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from common import DUMMY_KEY, PROMPT, TOKEN, Agent, Record

# Not a canary: the model's own output, which must come back byte-identical.
MODEL_EMAIL = "support@vendor.test"
MCP_TOOL = "mcp__canary__lookup"

SENT: list[list[dict[str, Any]]] = []  # assistant turns the upstream sent in the main loop


def setup(root: Path, server: dict[str, Any], transparent: bool) -> dict[str, str]:
    home = root / "home" / ".claude"
    home.mkdir(parents=True)
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"canary": server}}))
    if not transparent:
        # A base URL in Claude Code's own settings outranks its environment;
        # privyx run must reach it anyway (nothing answers on port 9).
        settings = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}
        (home / "settings.json").write_text(json.dumps(settings))
    return {
        "CLAUDE_CONFIG_DIR": str(home),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def args(root: Path) -> list[str]:
    # Only the two calls the loop makes are allowed: a real model picks its own.
    return ["-p", PROMPT, "--mcp-config", str(root / "mcp.json"), "--strict-mcp-config",
            "--allowedTools", MCP_TOOL, "Bash(cat notes.txt)"]  # fmt: skip


def is_main(body: dict[str, Any]) -> bool:
    """Whether ``body`` is a turn of the agent loop (not a title/summary side call)."""
    return any(t.get("name") == MCP_TOOL for t in body.get("tools", []))


def script(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Content blocks for this turn of the scripted agent loop."""
    if not is_main(body):
        return [{"type": "text", "text": "ok"}]
    done = sum(
        block.get("type") == "tool_result"
        for m in body.get("messages", [])
        if isinstance(m.get("content"), list)
        for block in m["content"]
    )

    def tokens(value: Any) -> str:
        return " ".join(dict.fromkeys(TOKEN.findall(json.dumps(value)))) or "none"

    if done >= 2:
        return [{"type": "text", "text": f"Echo: {tokens(body)}"}]
    # Mid-loop, mention only what the conversation said, not the system prompt,
    # as a model would — echoing a system value would mask its renumbering.
    seen = tokens(body.get("messages"))
    thinking = {
        "type": "thinking",
        "thinking": f"Noting {seen}; vendor contact is {MODEL_EMAIL}.",
        "signature": f"sig-e2e-{done + 1}",
    }
    call: dict[str, Any] = (
        {"name": MCP_TOOL, "input": {"query": seen}}
        if done == 0
        else {"name": "Bash", "input": {"command": "cat notes.txt", "description": "Read notes"}}
    )
    blocks = [thinking, {"type": "tool_use", "id": f"toolu_e2e_{done + 1}", **call}]
    SENT.append(blocks)
    return blocks


def message(content: list[dict[str, Any]], stop: str | None) -> dict[str, Any]:
    return {
        "id": "msg_e2e",
        "type": "message",
        "role": "assistant",
        "model": "claude-e2e",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def sse(blocks: list[dict[str, Any]], stop: str) -> bytes:
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message([], None)}]
    for i, block in enumerate(blocks):
        if block["type"] == "tool_use":
            start: dict[str, Any] = {**block, "input": {}}
            kind, key, payload = "input_json_delta", "partial_json", json.dumps(block["input"])
        elif block["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            kind, key, payload = "thinking_delta", "thinking", block["thinking"]
        else:
            start = {"type": "text", "text": ""}
            kind, key, payload = "text_delta", "text", block["text"]
        events.append({"type": "content_block_start", "index": i, "content_block": start})
        for j in range(0, len(payload), 5):  # 5-char deltas: tokens straddle frames
            delta = {"type": kind, key: payload[j : j + 5]}
            events.append({"type": "content_block_delta", "index": i, "delta": delta})
        if block["type"] == "thinking":
            delta = {"type": "signature_delta", "signature": block["signature"]}
            events.append({"type": "content_block_delta", "index": i, "delta": delta})
        events.append({"type": "content_block_stop", "index": i})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def fake(path: str, body: dict[str, Any]) -> tuple[bytes, str]:
    """The scripted Anthropic API's reply to one request."""
    if path.endswith("/count_tokens"):
        return json.dumps({"input_tokens": 1}).encode(), "application/json"
    blocks = script(body)
    stop = "tool_use" if blocks[-1]["type"] == "tool_use" else "end_turn"
    if body.get("stream"):
        return sse(blocks, stop), "text/event-stream"
    return json.dumps(message(blocks, stop)).encode(), "application/json"


def roundtrip(bodies: list[dict[str, Any]]) -> list[str]:
    """Where an echoed assistant turn differs from what the upstream sent."""
    field = {"thinking": "thinking", "tool_use": "input", "text": "text"}
    problems: dict[str, None] = {}
    for body in bodies:
        echoed = [m["content"] for m in body["messages"] if m.get("role") == "assistant"]
        for turn, (sent, back) in enumerate(zip(SENT, echoed, strict=False), 1):
            back = back if isinstance(back, list) else [{"type": "text", "text": back}]
            for block in sent:
                key = field[block["type"]]
                got = next((b.get(key) for b in back if b.get("type") == block["type"]), None)
                if got != block[key]:
                    problems[f"turn {turn} {block['type']}.{key}\n      sent {block[key]!r}"
                             f"\n      back {got!r}"] = None  # fmt: skip
    return list(problems)


def checks(records: list[Record]) -> Iterator[tuple[bool, str, str]]:
    keys = {r["headers"].get("x-api-key") for r in records}
    yield keys == {DUMMY_KEY}, "client key relayed", f"{len(records)} turn(s)"
    # The provider rejects a thinking block whose text no longer matches its signature.
    problems = roundtrip([r["body"] for r in records])
    detail = "".join([f"{len(SENT)} turn(s) sent", *(f"\n      {p}" for p in problems)])
    yield not problems, "echoed turns byte-identical", detail


AGENT = Agent(
    creds=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    endpoint="/v1/messages",
    stable=("system", "tools"),
    setup=setup,
    args=args,
    point=lambda url: ({"ANTHROPIC_BASE_URL": url}, []),
    main=lambda r: bool(r["body"]) and is_main(r["body"]),
    fake=fake,
    checks=checks,
)
