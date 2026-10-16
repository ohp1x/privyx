"""Keep the offline Codex tool loop verifiable without installing either CLI."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from privyx.streaming.sse import SSEDecoder


@pytest.fixture
def codex_harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / "scripts" / "test-e2e"))
    return importlib.import_module("codex")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("shell", ["shell_command", "exec_command", "shell"])
def test_codex_fake_runs_the_mcp_and_file_loop(
    codex_harness: ModuleType, stream: bool, shell: str
) -> None:
    token = "<PRIVYX_EMAIL_1>"
    body = {
        "tools": [
            {"type": "function", "name": "mcp__canary__lookup"},
            {"type": "function", "name": shell},
        ],
        "input": [{"role": "user", "content": token}],
        "stream": stream,
    }
    for turn in range(3):
        raw, content_type = codex_harness.fake("/v1/responses", body)
        if stream:
            assert content_type == "text/event-stream"
            events = [json.loads(e.data) for e in SSEDecoder().feed(raw.decode())]
            output = events[-1]["response"]["output"][0]
            prefix = "response.output_text" if turn == 2 else "response.function_call_arguments"
            deltas = [e["delta"] for e in events if e["type"] == prefix + ".delta"]
            assert all(len(part) <= 5 for part in deltas)
            expected = output["content"][0]["text"] if turn == 2 else output["arguments"]
            assert "".join(deltas) == expected
        else:
            assert content_type == "application/json"
            output = json.loads(raw)["output"][0]
        if turn == 0:
            assert output["name"] == "mcp__canary__lookup"
            assert json.loads(output["arguments"]) == {"query": token}
        elif turn == 1:
            assert output["name"] == shell
            args = json.loads(output["arguments"])
            assert args == (
                {"command": ["cat", "notes.txt"]}
                if shell == "shell"
                else {"cmd" if shell == "exec_command" else "command": "cat notes.txt"}
            )
        else:
            assert output["content"][0]["text"] == f"Echo: {token}"
            break
        body["input"].extend(
            [
                output,
                {"type": "function_call_output", "call_id": output["call_id"], "output": token},
            ]
        )


def test_codex_loop_check_rejects_failed_shell_output(codex_harness: ModuleType) -> None:
    records = [
        {
            "headers": {"Authorization": "Bearer sk-ant-e2e-dummy"},
            "body": {
                "input": [
                    {
                        "type": "function_call_output",
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


def test_codex_transparent_mode_sets_environment_and_config(codex_harness: ModuleType) -> None:
    env, args = codex_harness.AGENT.point("http://127.0.0.1:8123")
    assert env == {"OPENAI_BASE_URL": "http://127.0.0.1:8123/v1"}
    assert args == ["-c", 'openai_base_url="http://127.0.0.1:8123/v1"']
