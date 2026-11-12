"""Codex (``codex exec``) and a scripted Responses API for its tool loop."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from common import DUMMY_KEY, PROMPT, TOKEN, Agent, Record


def offered(body: dict[str, Any]) -> set[str]:
    """Names of the tools in ``body``, also those Codex sends as an ``input`` item."""
    tools = list(body.get("tools", []))
    for item in body.get("input", []):
        if item.get("type") == "additional_tools":
            tools += item.get("tools", [])
    # A namespace holds its tools; a plain tool stands for itself.
    return {t.get("name") for tool in tools for t in tool.get("tools", [tool])}


def script(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask for MCP lookup, read the workspace, then echo the received tokens."""
    if "exec" not in offered(body):
        raise ValueError("Codex did not advertise its exec tool")
    items = body.get("input", [])
    done = sum(item.get("type") == "custom_tool_call_output" for item in items)
    tokens = " ".join(dict.fromkeys(TOKEN.findall(json.dumps(body))))
    if done >= 2:
        return [
            {
                "id": "msg_e2e",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": f"Echo: {tokens}", "annotations": []}],
            }
        ]
    # Every tool but exec, the deferred MCP one too, is called from its JavaScript.
    code = (
        f"text(await tools.mcp__canary__lookup({{ query: {json.dumps(tokens)} }}))"
        if done == 0
        else 'text(await tools.exec_command({ cmd: "cat notes.txt" }))'
    )
    return [
        {
            "type": "custom_tool_call",
            "id": f"ctc_e2e_{done}",
            "call_id": f"call_e2e_{done}",
            "name": "exec",
            "input": code,
            "status": "completed",
        }
    ]


def fake(path: str, body: dict[str, Any]) -> tuple[bytes, str]:
    if path.endswith("/input_tokens"):
        return json.dumps({"input_tokens": 1}).encode(), "application/json"
    output = script(body)
    response = {
        "id": "resp_e2e",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": body.get("model", "codex-e2e"),
        "output": output,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    if not body.get("stream"):
        return json.dumps(response).encode(), "application/json"
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        }
    ]
    for index, item in enumerate(output):
        is_call = item["type"] == "custom_tool_call"
        initial = {**item, "input": ""} if is_call else {**item, "content": []}
        events.append(
            {"type": "response.output_item.added", "output_index": index, "item": initial}
        )
        text = item["input"] if is_call else item["content"][0]["text"]
        prefix = "response.custom_tool_call_input" if is_call else "response.output_text"
        common = {"item_id": item["id"], "output_index": index}
        if not is_call:
            common["content_index"] = 0
            events.append(
                {
                    "type": "response.content_part.added",
                    **common,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }
            )
        for start in range(0, len(text), 5):
            events.append({"type": f"{prefix}.delta", **common, "delta": text[start : start + 5]})
        events.append({"type": f"{prefix}.done", **common, "input" if is_call else "text": text})
        if not is_call:
            events.append(
                {"type": "response.content_part.done", **common, "part": item["content"][0]}
            )
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": response})
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': i})}\n\n"
        for i, event in enumerate(events)
    ).encode(), "text/event-stream"


def checks(records: list[Record]) -> Iterator[tuple[bool, str, str]]:
    bodies = [r["body"] for r in records]
    outputs = [
        item
        for body in bodies
        for item in body.get("input", [])
        if item.get("type") == "custom_tool_call_output"
    ]
    yield (
        len(bodies) >= 3
        and any(
            item.get("call_id") == "call_e2e_1" and "VPN gateway" in str(item.get("output", ""))
            for item in outputs
        ),
        "MCP and shell loop completed",
        (f"{len(bodies)} turn(s), {len(outputs)} echoed tool result(s)"),
    )
    keys = {
        next((v for k, v in r["headers"].items() if k.lower() == "authorization"), None)
        for r in records
    }
    yield keys == {f"Bearer {DUMMY_KEY}"}, "client key relayed", f"{len(records)} turn(s)"


def setup(root: Path, server: dict[str, Any], transparent: bool) -> dict[str, str]:
    home = root / "home" / ".codex"
    home.mkdir(parents=True)
    # Its tools run unasked, as Claude's --allowedTools lets them: codex exec cannot ask.
    server = {**server, "default_tools_approval_mode": "approve"}
    # A JSON string or list of strings is also valid TOML.
    toml = "".join(f"{k} = {json.dumps(v)}\n" for k, v in server.items())
    (home / "config.toml").write_text(f"[mcp_servers.canary]\n{toml}")
    return {"CODEX_HOME": str(home)}


# Codex sends its instructions and tools as ``input`` items for some models, so
# no key is checked for stability yet.
AGENT = Agent(
    creds=("CODEX_API_KEY",),
    endpoint="/v1/responses",
    stable=(),
    setup=setup,
    args=lambda _: ["exec", "--sandbox", "read-only", PROMPT],
    point=lambda url: (
        {"OPENAI_BASE_URL": url + "/v1"},
        ["-c", f"openai_base_url={json.dumps(url + '/v1')}"],
    ),
    main=lambda r: r["body"] is not None and r["path"].startswith("/v1/responses"),
    fake=fake,
    checks=checks,
)
