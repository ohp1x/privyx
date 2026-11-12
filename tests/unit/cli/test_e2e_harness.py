"""Keep the offline agent tool loops verifiable without installing either CLI."""

from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from privyx.streaming.sse import SSEDecoder


def _harness(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / "scripts" / "test-e2e"))
    return importlib.import_module(name)


@pytest.fixture
def codex_harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _harness(monkeypatch, "codex")


@pytest.fixture
def claude_harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _harness(monkeypatch, "claude")


@pytest.fixture
def run_harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _harness(monkeypatch, "run")


@pytest.mark.parametrize("stream", [False, True])
def test_codex_fake_runs_the_mcp_and_file_loop(codex_harness: ModuleType, stream: bool) -> None:
    token = "<PRIVYX_EMAIL_1>"
    # Codex sends its tools as an input item; every one but exec is called from it.
    functions = {
        "type": "namespace",
        "name": "functions",
        "tools": [{"type": "custom", "name": "exec"}],
    }
    body = {
        "input": [
            {"type": "additional_tools", "role": "developer", "tools": [functions]},
            {"role": "user", "content": token},
        ],
        "stream": stream,
    }
    for turn in range(3):
        raw, content_type = codex_harness.fake("/v1/responses", body)
        if stream:
            assert content_type == "text/event-stream"
            events = [json.loads(e.data) for e in SSEDecoder().feed(raw.decode())]
            output = events[-1]["response"]["output"][0]
            prefix = "response.output_text" if turn == 2 else "response.custom_tool_call_input"
            deltas = [e["delta"] for e in events if e["type"] == prefix + ".delta"]
            assert all(len(part) <= 5 for part in deltas)
            expected = output["content"][0]["text"] if turn == 2 else output["input"]
            assert "".join(deltas) == expected
        else:
            assert content_type == "application/json"
            output = json.loads(raw)["output"][0]
        if turn == 0:
            assert output["name"] == "exec"
            assert (
                output["input"] == f'text(await tools.mcp__canary__lookup({{ query: "{token}" }}))'
            )
        elif turn == 1:
            assert output["name"] == "exec"
            assert output["input"] == 'text(await tools.exec_command({ cmd: "cat notes.txt" }))'
        else:
            assert output["content"][0]["text"] == f"Echo: {token}"
            break
        body["input"].extend(
            [
                output,
                {"type": "custom_tool_call_output", "call_id": output["call_id"], "output": token},
            ]
        )


def test_codex_fake_needs_the_exec_tool(codex_harness: ModuleType) -> None:
    body = {"tools": [{"type": "function", "name": "shell"}], "input": []}
    with pytest.raises(ValueError, match="exec"):
        codex_harness.fake("/v1/responses", body)


def test_codex_loop_check_rejects_failed_shell_output(codex_harness: ModuleType) -> None:
    records = [
        {
            "headers": {"Authorization": "Bearer sk-ant-e2e-dummy"},
            "body": {
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_e2e_1",
                        "output": "exit code: 1",
                    }
                ]
            },
        }
    ] * 3
    assert next(codex_harness.checks(records))[0] is False
    records[0]["body"]["input"][0]["output"] = "VPN gateway <PRIVYX_IP_ADDRESS_1>"
    assert next(codex_harness.checks(records))[0] is True


def test_codex_setup_approves_the_mcp_tools(codex_harness: ModuleType, tmp_path: Path) -> None:
    codex_harness.setup(tmp_path, {"command": "python3", "args": ["server.py"]}, False)
    config = tomllib.loads((tmp_path / "home" / ".codex" / "config.toml").read_text())
    server = config["mcp_servers"]["canary"]
    assert server == {
        "command": "python3",
        "args": ["server.py"],
        "default_tools_approval_mode": "approve",
    }


def test_codex_transparent_mode_sets_environment_and_config(codex_harness: ModuleType) -> None:
    env, args = codex_harness.AGENT.point("http://127.0.0.1:8123")
    assert env == {"OPENAI_BASE_URL": "http://127.0.0.1:8123/v1"}
    assert args == ["-c", 'openai_base_url="http://127.0.0.1:8123/v1"']


@pytest.mark.parametrize("deferred", [True, False])
def test_claude_fake_loads_a_deferred_mcp_tool_first(
    claude_harness: ModuleType, deferred: bool
) -> None:
    token = "<PRIVYX_EMAIL_1>"
    search = {"name": "ToolSearch", "input_schema": {"type": "object"}}
    lookup = {"name": "mcp__canary__lookup", "input_schema": {"type": "object"}}
    tools = [search] if deferred else [search, lookup]
    body = {"tools": tools, "messages": [{"role": "user", "content": token}]}
    calls = []
    for _ in range(4):
        blocks = claude_harness.script(body)
        if blocks[-1]["type"] != "tool_use":
            break
        call = blocks[-1]
        calls.append((call["name"], call["input"]))
        if call["name"] == "ToolSearch":  # Claude Code adds the tool it loads, deferred
            body["tools"] = [search, {**lookup, "defer_loading": True}]
        result = {"type": "tool_result", "tool_use_id": call["id"], "content": token}
        body["messages"] += [
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [result]},
        ]
    bash = ("Bash", {"command": "cat notes.txt", "description": "Read notes"})
    loop = [("mcp__canary__lookup", {"query": token}), bash]
    load = ("ToolSearch", {"query": "select:mcp__canary__lookup", "max_results": 1})
    assert calls == ([load, *loop] if deferred else loop)
    assert blocks == [{"type": "text", "text": f"Echo: {token}"}]


def test_tools_drift_allows_a_newly_loaded_deferred_tool(run_harness: ModuleType) -> None:
    bash, write = {"name": "Bash"}, {"name": "Write"}
    placeholder = {"name": "Placeholder", "defer_loading": True}
    lookup = {"name": "lookup", "description": "<PRIVYX_ORGANIZATION_1>", "defer_loading": True}
    # Claude Code moves the deferred tools after the rest once it loads one.
    turns = [{"tools": [bash, placeholder, write]}, {"tools": [bash, write, placeholder, lookup]}]
    assert run_harness.drift(turns, "tools") == ""
    renumbered = {**lookup, "description": "<PRIVYX_ORGANIZATION_2>"}
    assert run_harness.drift([*turns, {"tools": [bash, write, placeholder, renumbered]}], "tools")
    assert run_harness.drift([turns[0], {"tools": [bash, placeholder]}], "tools")


def test_auto_mode_is_refused_for_an_agent_without_one(run_harness: ModuleType) -> None:
    # Codex has no such mode: a run that claimed to cover it would check nothing new.
    assert run_harness.AGENTS["claude"].auto
    with pytest.raises(SystemExit, match="codex has no auto permission mode"):
        run_harness.main("codex", False, "", None, auto=True)
