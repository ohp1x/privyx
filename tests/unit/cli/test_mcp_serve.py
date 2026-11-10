"""MCP server tool and protocol tests."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from privyx.cli.commands.mcp_serve import _handle, _respond, _tool_result
from privyx.cli.main import cli


def _resolved(root):
    return Path(root or Path.cwd()).resolve()


async def _run(name, arguments, config=None, root=None, engine=None):
    """A tools/call result, decoded: (value or message, is_error)."""
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    settings = load_config(str(config) if config else None)
    built, close = await build_engine(settings)
    try:
        result = await _tool_result(
            {"name": name, "arguments": arguments},
            engine or built,
            settings,
            _resolved(root),
        )
    finally:
        await close()
    text = result["content"][0]["text"]
    if result.get("isError"):
        return text, True
    return json.loads(text), False


def _sqlite(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"vault:\n  type: sqlite\n  dsn: sqlite+aiosqlite:///{tmp_path / 'v.db'}\n")
    return config


class _KeepAll:
    async def decide(self, detection, context):
        return detection


def _cli(*args, env=None):
    return subprocess.Popen(
        [sys.executable, "-c", "from privyx.cli.main import cli; cli()", "mcp", "serve", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


# --------------------------------------------------------------------------
# privyx_scan


@pytest.mark.asyncio
async def test_scan_does_not_return_values():
    result, error = await _run("privyx_scan", {"text": "ana@example.com"})
    assert not error
    assert result["findings"][0]["category"] == "EMAIL"
    assert "ana@example.com" not in str(result)


@pytest.mark.asyncio
async def test_scan_filters_categories_before_merging():
    from privyx.core.result import Detection, Span

    class Detector:
        async def detect(self, text, context):
            return Detection([Span(0, 4, "EMAIL", text), Span(0, 4, "PHONE", text)])

    engine = SimpleNamespace(detector=Detector(), policy=_KeepAll())
    result, _ = await _run("privyx_scan", {"text": "test", "categories": ["phone"]}, engine=engine)
    assert result == {"filtered_out": 1, "findings": [{"start": 0, "end": 4, "category": "PHONE"}]}


@pytest.mark.asyncio
async def test_scan_applies_the_policy(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("policy:\n  type: strict\n  allowed: [PHONE]\n")
    result, _ = await _run("privyx_scan", {"text": "ana@example.com"}, config)
    assert result == {"findings": []}


# --------------------------------------------------------------------------
# privyx_precommit_check


@pytest.mark.asyncio
async def test_precommit_checks_added_lines():
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,1 +1,2 @@\n existing\n+password = hunter2\n"
    result, _ = await _run("privyx_precommit_check", {"git_diff": diff})
    assert not result["ok"]
    assert result["findings"][0]["line"] == 2
    assert "hunter2" not in str(result)


@pytest.mark.asyncio
async def test_precommit_checks_diff_without_prefix():
    diff = (
        "diff --git notes.txt notes.txt\n--- notes.txt\n+++ notes.txt\n"
        "@@ -0,0 +1,2 @@\n+password = hunter2\n+<PRIVYX_EMAIL_1>\n"
    )
    result, _ = await _run("privyx_precommit_check", {"git_diff": diff})
    assert result["findings"] == [
        {"path": "notes.txt", "line": 1, "category": "SECRET"},
        {"path": "notes.txt", "line": 2, "category": "placeholder"},
    ]


@pytest.mark.asyncio
async def test_precommit_uses_server_token_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('token:\n  namespace: SAMPLE\n  format: "[[{namespace}:{type}:{id}]]"\n')
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+[[SAMPLE:EMAIL:1]]\n"
    result, _ = await _run("privyx_precommit_check", {"git_diff": diff}, config)
    assert result == {
        "ok": False,
        "findings": [{"path": "a", "line": 1, "category": "placeholder"}],
    }


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        pytest.param(
            "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "new.txt", "line": 1, "category": "SECRET"}],
            id="deleted-then-added",
        ),
        pytest.param(
            "--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+ok\n"
            "--- a/y.txt\n+++ b/y.txt\n@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "y.txt", "line": 1, "category": "SECRET"}],
            id="two-files",
        ),
        pytest.param(
            "--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,3 @@\n one\n\n+password = hunter2\n",
            [{"path": "x.txt", "line": 3, "category": "SECRET"}],
            id="context-without-space",
        ),
        pytest.param(
            "--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+ok\n+password = hunter2\n",
            [{"path": "x.txt", "line": 2, "category": "SECRET"}],
            id="count-too-small",
        ),
        pytest.param(
            "--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1,5 @@\n+ok\n"
            "--- a/y.txt\n+++ b/y.txt\n@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "y.txt", "line": 1, "category": "SECRET"}],
            id="count-too-large",
        ),
        pytest.param(
            "--- a/old.txt\n+++ /dev/null\n@@ -1,5 +0,0 @@\n-a\n-b\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "new.txt", "line": 1, "category": "SECRET"}],
            id="count-too-large-after-deleted-file",
        ),
        pytest.param(
            "@@ -1 +1 @@\n-x\n+password = hunter2\n",
            [{"path": "(unknown)", "line": 1, "category": "SECRET"}],
            id="hunk-without-header",
        ),
        pytest.param(
            "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n--- x\n+++ password = hunter2\n",
            [{"path": "f.txt", "line": 2, "category": "SECRET"}],
            id="header-shaped-lines-after-short-count",
        ),
        pytest.param(
            "--- f.txt\t2024-01-01 00:00:00\n+++ f.txt\t2024-01-02 00:00:00\n"
            "@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "f.txt", "line": 1, "category": "SECRET"}],
            id="diff-u-timestamps",
        ),
        pytest.param(
            "--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1,2 @@\n-a\n+b\n"
            "--- a/two.txt\n+++ b/two.txt\n@@ -0,0 +1 @@\n+password = hunter2\n",
            # The header used up the count exactly, so it reads as a removed
            # and an added line, as it would in a correct diff: the label is
            # wrong, the secret is still found.
            [{"path": "one.txt", "line": 1, "category": "SECRET"}],
            id="count-too-large-by-the-header",
        ),
        pytest.param(
            "diff --cc f\nindex f2ad6c7,6178079..0000000\n--- a/f\n+++ b/f\n"
            "@@@ -1,1 -1,1 +1,3 @@@\n- c\n++ok\n +password = hunter2\n+ b\n",
            [{"path": "f", "line": 2, "category": "SECRET"}],
            id="combined-merge-diff",
        ),
        pytest.param(
            "--- a.py\r\n+++ a.py\r\n@@ -0,0 +1 @@\r\n+password = hunter2\r\n",
            [{"path": "a.py", "line": 1, "category": "SECRET"}],
            id="crlf",
        ),
        pytest.param(
            "--- b/config.py\n+++ b/config.py\n@@ -0,0 +1 @@\n+password = hunter2\n",
            [{"path": "b/config.py", "line": 1, "category": "SECRET"}],
            id="plain-diff-of-a-b-directory",
        ),
        pytest.param(
            "\x1b[1mdiff --git a/x b/x\x1b[m\n\x1b[1m--- a/x\x1b[m\n\x1b[1m+++ b/x\x1b[m\n"
            "\x1b[36m@@ -0,0 +1 @@\x1b[m\n\x1b[32m+\x1b[m\x1b[32mpassword = hunter2\x1b[m\n",
            [{"path": "x", "line": 1, "category": "SECRET"}],
            id="colored",
        ),
    ],
)
@pytest.mark.asyncio
async def test_precommit_reads_plain_unified_diffs(diff, expected):
    result, _ = await _run("privyx_precommit_check", {"git_diff": diff})
    assert result["findings"] == expected


# --------------------------------------------------------------------------
# privyx_sanitize_file


@pytest.mark.asyncio
async def test_sanitize_never_overwrites_and_leaves_no_session(tmp_path):
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    config = _sqlite(tmp_path)
    (tmp_path / "input.txt").write_text("ana@example.com")
    args = {"path": "input.txt", "output_path": "output.txt"}
    result, error = await _run("privyx_sanitize_file", args, config, tmp_path)
    assert not error and result["output_path"] == "output.txt"
    assert "ana@example.com" not in (tmp_path / "output.txt").read_text()

    for output in ("output.txt", "input.txt"):
        message, error = await _run(
            "privyx_sanitize_file", {**args, "output_path": output}, config, tmp_path
        )
        assert error and message == "output file already exists"
    engine, close = await build_engine(load_config(str(config)))
    try:
        assert [s.session_id for s in await engine.vault.list_sessions()] == [result["session_id"]]
    finally:
        await close()


@pytest.mark.asyncio
async def test_sanitize_session_restores_with_persistent_vault(tmp_path):
    from privyx.cli.commands.mask import _unmask_async

    (tmp_path / "input.txt").write_text("ana@example.com")
    config = _sqlite(tmp_path)
    result, _ = await _run("privyx_sanitize_file", {"path": "input.txt"}, config, tmp_path)
    assert "warning" not in result
    restored = await _unmask_async(
        result["text"], "text", (), None, result["session_id"], str(config)
    )
    assert restored == "ana@example.com"


@pytest.mark.asyncio
async def test_sanitize_memory_vault_warns_and_keeps_nothing(tmp_path):
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    (tmp_path / "input.txt").write_text("ana@example.com")
    settings = load_config(None)
    engine, close = await build_engine(settings)
    try:
        result = await _tool_result(
            {"name": "privyx_sanitize_file", "arguments": {"path": "input.txt"}},
            engine,
            settings,
            tmp_path.resolve(),
        )
        value = json.loads(result["content"][0]["text"])
        assert "lost after this call" in value["warning"]
        assert "session_id" not in value
        assert await engine.vault.list_sessions() == []
    finally:
        await close()


@pytest.mark.asyncio
async def test_sanitize_refuses_paths_outside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("ana@example.com")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    for args in (
        {"path": str(tmp_path / "secret.txt")},
        {"path": "../secret.txt"},
        {"path": "link.txt"},
        {"path": "link.txt", "output_path": "../out.txt"},
    ):
        message, error = await _run("privyx_sanitize_file", args, root=root)
        assert error and message == "path is outside the server's root", args
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_sanitize_masks_json(tmp_path):
    (tmp_path / "data.json").write_text('{"email": "ana@example.com", "n": 1}')
    result, _ = await _run("privyx_sanitize_file", {"path": "data.json"}, root=tmp_path)
    value = json.loads(result["text"])
    assert value["n"] == 1 and "ana@example.com" not in result["text"]


@pytest.mark.asyncio
async def test_tool_errors_say_what_is_wrong(tmp_path):
    (tmp_path / "bad.json").write_text('{"email": "ana@example.com"')
    cases = [
        ({"path": "missing.txt"}, "file not found"),
        ({"path": "bad.json"}, "file is not valid JSON"),
        ({"path": 1}, "path and output_path must be strings"),
        ({"path": "bad.json", "format": "xml"}, "format must be auto, text, json, or jsonl"),
        ({"path": "."}, "path is not a regular file"),
        ({"path": "fifo"}, "path is not a regular file"),
        ({"path": "bad.json", "output_path": "no/out.json"}, "output directory does not exist"),
        ({"path": "loop"}, "invalid path"),
        ({"path": "a\x00b"}, "invalid path"),
    ]
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    os.mkfifo(tmp_path / "fifo")
    for args, expected in cases:
        assert await _run("privyx_sanitize_file", args, root=tmp_path) == (expected, True)
    message, error = await _run("privyx_scan", {"text": "x", "categories": []})
    assert error and message == "categories must be a non-empty array of strings"


@pytest.mark.asyncio
async def test_unexpected_tool_error_logs_only_its_type(capsys):
    class Detector:
        async def detect(self, text, context):
            raise RuntimeError(f"boom {text}")

    engine = SimpleNamespace(detector=Detector(), policy=_KeepAll())
    message, error = await _run("privyx_scan", {"text": "ana@example.com"}, engine=engine)
    assert error and message == "Tool failed; see the server's log."
    err = capsys.readouterr().err
    assert "privyx_scan failed: RuntimeError" in err
    assert "ana@example.com" not in err


# --------------------------------------------------------------------------
# Protocol


async def _answers(*lines):
    from privyx.config.loader import load_config

    settings = load_config(None)
    return [await _respond(line, None, settings, Path.cwd()) for line in lines]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_protocol_errors():
    request = {"jsonrpc": "2.0", "id": 1}
    answers = await _answers(
        "{not json",
        "[]",
        json.dumps({**request, "method": "nope"}),
        json.dumps({**request, "method": "tools/call", "params": {"name": "nope"}}),
        json.dumps({**request, "method": "tools/list", "params": []}),
        json.dumps({**request, "result": {}}),
        json.dumps({"id": 9, "method": "ping"}),
        json.dumps({**request, "method": "tools/call", "params": {"name": ["x"]}}),
        "[" * 200_000,
    )
    assert [a and a["error"]["code"] for a in answers] == [
        -32700,
        -32600,
        -32601,
        -32602,
        -32602,
        None,
        -32600,
        -32602,
        -32700,
    ]
    assert answers[6]["id"] == 9  # no "jsonrpc", but the caller still gets its id


@pytest.mark.asyncio
async def test_protocol_answers_batches():
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "nope"},
    ]
    (answer,) = await _answers(json.dumps(batch))
    assert [r["id"] for r in answer] == [1, 2]
    assert answer[0]["result"] == {}
    (answer,) = await _answers(json.dumps(batch[1:2]))
    assert answer is None


@pytest.mark.asyncio
async def test_protocol_negotiates_versions():
    def initialize(version):
        params = {"protocolVersion": version}
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})

    answers = await _answers(initialize("2024-11-05"), initialize("1999-01-01"))
    assert [a["result"]["protocolVersion"] for a in answers] == ["2024-11-05", "2025-06-18"]


def test_protocol_lists_tools_and_skips_notifications_and_blank_lines():
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "\n\n".join(map(json.dumps, messages)) + "\n\n"
    result = CliRunner().invoke(cli, ["mcp", "serve"], input=stdin)
    assert result.exit_code == 0, result.output
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert len(responses[1]["result"]["tools"]) == 3


def test_output_from_a_tool_goes_to_stderr(monkeypatch):
    async def noisy(*args):
        print("noise")
        return {"ok": True}

    monkeypatch.setattr("privyx.cli.commands.mcp_serve._call", noisy)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    request["params"] = {"name": "privyx_scan", "arguments": {}}
    result = CliRunner().invoke(cli, ["mcp", "serve"], input=json.dumps(request) + "\n")
    assert json.loads(result.stdout)["id"] == 1
    assert "noise" in result.stderr


def test_stdio_is_utf8_whatever_the_locale():
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    request["params"] = {"name": "privyx_scan", "arguments": {"text": "café ana@example.com"}}
    process = _cli(env=env)
    out, err = process.communicate(json.dumps(request, ensure_ascii=False).encode() + b"\n", 30)
    assert process.returncode == 0, err
    findings = json.loads(json.loads(out)["result"]["content"][0]["text"])["findings"]
    assert findings[0]["start"] == 5


def test_one_ctrl_c_stops_the_server():
    process = _cli()
    process.stdin.write(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline())["id"] == 1
    process.send_signal(signal.SIGINT)
    assert process.wait(10) == 130
    process.stdin.close()


def test_client_closing_stdout_exits_quietly():
    process = _cli()
    process.stdout.close()
    _, err = process.communicate(b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n' * 200, 30)
    assert process.returncode == 0
    assert err == b""


def test_lone_surrogate_and_internal_error_do_not_stop_the_server(monkeypatch):
    real = _handle

    async def flaky(message, *args):
        if message.get("id") == 2:
            raise RuntimeError("boom")
        return await real(message, *args)

    monkeypatch.setattr("privyx.cli.commands.mcp_serve._handle", flaky)
    lines = [
        '{"jsonrpc": "2.0", "id": "\\ud800", "method": "ping"}',
        '{"jsonrpc": "2.0", "id": 2, "method": "ping"}',
        '{"jsonrpc": "2.0", "id": 3, "method": "ping"}',
    ]
    result = CliRunner().invoke(cli, ["mcp", "serve"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0, result.output
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == ["\ud800", 2, 3]
    assert responses[1]["error"]["code"] == -32603


def test_output_below_python_goes_to_stderr():
    # A plugin that prints as it loads, through Python and straight to fd 1.
    script = (
        "import os, privyx.plugins.loader as loader\n"
        "real = loader.load_plugins\n"
        "def noisy(settings):\n"
        "    print('noise from python', flush=True)\n"
        "    os.write(1, b'noise from fd\\n')\n"
        "    return real(settings)\n"
        "loader.load_plugins = noisy\n"
        "from privyx.cli.main import cli\n"
        "cli()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = process.communicate(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n', 30)
    assert process.returncode == 0, err
    assert [json.loads(line)["id"] for line in out.splitlines()] == [1]
    assert b"noise from python" in err and b"noise from fd" in err


@pytest.mark.asyncio
async def test_sanitize_saves_the_session_once(tmp_path):
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    config = _sqlite(tmp_path)
    leaves = {f"k{i}": f"user{i}@example.com" for i in range(50)}
    (tmp_path / "data.json").write_text(json.dumps(leaves))
    settings = load_config(str(config))
    engine, close = await build_engine(settings)
    saves = 0
    save = engine.vault.save

    async def counted(*args, **kwargs):
        nonlocal saves
        saves += 1
        return await save(*args, **kwargs)

    engine.vault.save = counted
    try:
        result = await _tool_result(
            {"name": "privyx_sanitize_file", "arguments": {"path": "data.json"}},
            engine,
            settings,
            tmp_path.resolve(),
        )
    finally:
        await close()
    assert not result.get("isError")
    assert saves == 1


def test_a_response_too_deep_to_write_does_not_stop_the_server(monkeypatch):
    # Deeper than any stack: from 3.14 the limit depends on the stack's size.
    deep: list = []
    for _ in range(1_000_000):
        deep = [deep]

    async def handle(message, *args):
        return {"jsonrpc": "2.0", "id": deep if message["id"] == 1 else message["id"], "result": {}}

    monkeypatch.setattr("privyx.cli.commands.mcp_serve._handle", handle)
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}) for i in (1, 2)]
    result = CliRunner().invoke(cli, ["mcp", "serve"], input="\n".join(lines) + "\n")
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(r["id"], "error" in r) for r in responses] == [(None, True), (2, False)]


def test_the_mcp_group_has_serve():
    from privyx.cli.commands.mcp import mcp

    assert "serve" in mcp.commands


def test_nan_ids_get_valid_json():
    def strict(constant):
        raise AssertionError(f"not JSON: {constant}")

    lines = [
        '{"jsonrpc": "2.0", "id": NaN, "method": "ping"}',
        '{"jsonrpc": "2.0", "id": 1e999, "method": "ping"}',
    ]
    result = CliRunner().invoke(cli, ["mcp", "serve"], input="\n".join(lines) + "\n")
    responses = [json.loads(line, parse_constant=strict) for line in result.stdout.splitlines()]
    assert [r["error"]["code"] for r in responses] == [-32603, -32603]


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_the_reason(tmp_path):
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine
    from privyx.core.errors import VaultError

    (tmp_path / "bad.json").write_text("{")
    settings = load_config(None)
    engine, close = await build_engine(settings)

    async def broken(*args, **kwargs):
        raise VaultError("vault down")

    engine.delete_session = broken
    try:
        result = await _tool_result(
            {"name": "privyx_sanitize_file", "arguments": {"path": "bad.json"}},
            engine,
            settings,
            tmp_path.resolve(),
        )
    finally:
        await close()
    assert result["content"][0]["text"] == "file is not valid JSON"


@pytest.mark.asyncio
async def test_failed_write_leaves_no_output(tmp_path):
    # A lone surrogate survives masking but cannot be written as UTF-8.
    (tmp_path / "data.json").write_text('{"a": "\\ud800"}')
    args = {"path": "data.json", "output_path": "out.json"}
    _, error = await _run("privyx_sanitize_file", args, root=tmp_path)
    assert error
    assert not (tmp_path / "out.json").exists()


@pytest.mark.asyncio
async def test_a_batch_answer_that_cannot_be_written_spares_the_others():
    from privyx.cli.commands.mcp_serve import _dumps

    batch = (
        '[{"jsonrpc": "2.0", "id": 1, "method": "ping"},'
        ' {"jsonrpc": "2.0", "id": NaN, "method": "ping"}]'
    )
    (answer,) = await _answers(batch)
    written = json.loads(_dumps(answer), parse_constant=lambda c: pytest.fail(c))
    assert [(r["id"], "error" in r) for r in written] == [(1, False), (None, True)]
