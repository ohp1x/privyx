"""Tests for `privyx mcp wrap`."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from privyx.cli.main import cli

#: A stand-in MCP server: logs what it receives to argv[1] and answers each
#: request with a value the client must not see.
SERVER = r"""
import json, os, sys
open(sys.argv[1] + ".pid", "w").write(str(os.getpid()))
log = open(sys.argv[1], "a")
for line in sys.stdin:
    log.write(line)
    log.flush()
    msg = json.loads(line)
    method = msg.get("method")
    if method == "exit":
        sys.exit(3)
    elif method == "log":
        print("plain log from bob@example.com", flush=True)
        print(json.dumps({"level": "info", "msg": "bob@example.com"}), flush=True)
        print(json.dumps({"method": "bob@example.com", "bob@example.com": 1}), flush=True)
    elif method == "keys":
        reply = {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"structuredContent": {"bob@example.com": {"status": "active"}}}}
        print(json.dumps(reply), flush=True)
    elif method == "later":
        import time
        time.sleep(3)
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": "bob@example.com"}
        print(json.dumps(reply), flush=True)
    elif method == "notes":
        for note in (
            {"method": "notifications/cancelled",
             "params": {"requestId": "ana@example.com", "reason": "ana@example.com"}},
            {"method": "notifications/progress",
             "params": {"progressToken": "ana@example.com", "progress": 1}},
            {"method": "sampling/createMessage", "id": 9,
             "params": {"_meta": {"progressToken": "ana@example.com"}}},
        ):
            print(json.dumps({"jsonrpc": "2.0", **note}), flush=True)
    elif method == "secret":
        content = {
            "password": 'my "pass"\nphrase',
            "passphrase": "prefix bob@example.com suffix",
            "a: password": "hunter2",
            "note": "hunter2",
        }
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"structuredContent": content}}
        print(json.dumps(reply), flush=True)
    elif method == "numbers":
        content = {"card": 4111111111111111, "password": 123456, "count": 3, "ratio": 0.5,
                   "ok": True, "cards": [4111111111111111]}
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"structuredContent": content}}
        print(json.dumps(reply), flush=True)
    elif method == "binary":
        blob = "////4111111111111111////"  # base64 that reads like a card number
        result = {
            "contents": [{"uri": "file:///a.bin", "blob": blob}],
            "content": [
                {"type": "image", "mimeType": "image/png", "data": blob},
                {"type": "audio", "mimeType": "audio/wav", "data": "bob@example.com"},
            ],
        }
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    elif method == "context":
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-03-26",
            "structuredContent": {
                "owner": "contact bob@example.com",
                "other": "bob@example.com",
                "employee": "EMP1234",
            },
        }}
        print(json.dumps(reply), flush=True)
    elif method == "secret-lists":
        content = {"passwords": ["hunter2", "letmein"], "password": [["s3cret"]]}
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"structuredContent": content}}
        print(json.dumps(reply), flush=True)
    elif method == "orphan":
        import subprocess
        child = (
            "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
        )
        subprocess.Popen([sys.executable, "-c", child, sys.argv[1] + ".child"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        while not os.path.exists(sys.argv[1] + ".child"):
            pass
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
    elif method == "stubborn":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
    elif method == "flood":
        line = json.dumps({"jsonrpc": "2.0", "method": "notify", "params": "x" * 100})
        for _ in range(20_000):
            print(line)
        sys.stdout.flush()
    elif method == "big":
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": "x" * 1_000_000}
        print(json.dumps(reply), flush=True)
    elif method == "raw":
        # A duplicate key: a parser keeps the second "text" only.
        content = '[{"type": "text", "text": "ana@example.com", "text": "ok"}]'
        line = '{"jsonrpc": "2.0", "id": %d, "result": {"content": %s}}\n'
        sys.stdout.write(line % (msg["id"], content))
        sys.stdout.flush()
    elif "id" in msg:
        text = "reply to bob@example.com"
        reply = {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"content": [{"type": "text", "text": text}]}}
        print(json.dumps(reply), flush=True)
"""


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "vault:\n"
        "  type: sqlite\n"
        f"  dsn: sqlite+aiosqlite:///{tmp_path / 'privyx.db'}\n"
        "detector:\n"
        "  type: regex\n",
        encoding="utf-8",
    )
    return path


def _wrap(config: Path, log: Path, *server: str) -> subprocess.Popen[bytes]:
    command = server or (sys.executable, "-c", SERVER, str(log))
    return subprocess.Popen(
        [sys.executable, "-c", "from privyx.cli.main import cli; cli()"]
        + ["mcp", "wrap", "-c", str(config), "--session", "s1", "--", *command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _send(proc: subprocess.Popen[bytes], method: str, **params: Any) -> None:
    assert proc.stdin is not None
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()


def _received(log: Path) -> list[Any]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _wait_for_server(log: Path) -> None:
    """Until the server has a request: wrap is past start-up, its SIGTERM handler set."""
    deadline = time.monotonic() + 30
    while not (log.exists() and log.read_text()):
        assert time.monotonic() < deadline, "the server never got the request"
        time.sleep(0.1)


def test_masks_replies_and_restores_requests(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        # Answered while the client keeps stdin open.
        _send(proc, "tools/call", name="read", arguments={})
        reply = json.loads(proc.stdout.readline())
        assert reply == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "reply to <PRIVYX_EMAIL_1>"}]},
        }
        _send(proc, "tools/call", name="send", arguments={"to": "<PRIVYX_EMAIL_1>"})
        proc.stdout.readline()
        assert _received(log)[1]["params"]["arguments"]["to"] == "bob@example.com"
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()


def test_masks_and_restores_object_keys(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "keys")
        reply = json.loads(proc.stdout.readline())
        assert reply["result"] == {"structuredContent": {"<PRIVYX_EMAIL_1>": {"status": "active"}}}
        _send(proc, "tools/call", arguments={"<PRIVYX_EMAIL_1>": "active"})
        proc.stdout.readline()
        assert _received(log)[1]["params"]["arguments"] == {"bob@example.com": "active"}
    finally:
        proc.kill()


def test_masks_a_value_its_key_says_is_secret(config: Path, tmp_path: Path) -> None:
    """Alone, ``my "pass"...`` is nothing; under ``password`` it is a secret."""
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "secret")
        content = json.loads(proc.stdout.readline())["result"]["structuredContent"]
        assert content == {
            "password": "<PRIVYX_SECRET_1>",
            # Whole, not just the email in it.
            "passphrase": "<PRIVYX_SECRET_2>",
            # Where the value starts is not found by searching for ": ".
            "a: password": "<PRIVYX_SECRET_3>",
            "note": "hunter2",
        }
        _send(proc, "tools/call", arguments={"password": "<PRIVYX_SECRET_1>"})
        proc.stdout.readline()
        assert _received(log)[1]["params"]["arguments"] == {"password": 'my "pass"\nphrase'}
    finally:
        proc.kill()


def test_masks_numbers_that_hold_a_value(config: Path, tmp_path: Path) -> None:
    """A card number sent as a JSON number; numbers that hold none stay numbers."""
    proc = _wrap(config, tmp_path / "server.log")
    assert proc.stdout is not None
    try:
        _send(proc, "numbers")
        content = json.loads(proc.stdout.readline())["result"]["structuredContent"]
        assert content == {
            "card": "<PRIVYX_CREDIT_CARD_1>",
            "password": "<PRIVYX_SECRET_2>",  # one counter for every type
            "count": 3,
            "ratio": 0.5,
            "ok": True,
            "cards": ["<PRIVYX_CREDIT_CARD_1>"],
        }
    finally:
        proc.kill()


def test_passes_base64_payloads_on_unread(config: Path, tmp_path: Path) -> None:
    proc = _wrap(config, tmp_path / "server.log")
    assert proc.stdout is not None
    try:
        _send(proc, "binary")
        result = json.loads(proc.stdout.readline())["result"]
        assert result["contents"][0]["blob"] == "////4111111111111111////"
        assert result["content"][0]["data"] == "////4111111111111111////"
        # Not base64: text, whatever the key.
        assert result["content"][1]["data"] == "<PRIVYX_EMAIL_1>"
    finally:
        proc.kill()


def test_value_spans_land_on_the_characters_json_escaped() -> None:
    """Every character, escaped or not, maps back to itself; a span cut inside
    an escape takes the character."""
    from privyx.cli.commands.mcp import _probe, _value_spans
    from privyx.core.result import Span

    key, value = "k", 'a"b\\c\nd\x01e\u2713'
    start = len(key) + 3
    for i, char in enumerate(value):
        at = start + len(json.dumps(value[:i], ensure_ascii=False)) - 2
        size = len(json.dumps(char, ensure_ascii=False)) - 2
        for low, high in ((at, at + size), (at + size - 1, at + size)):
            (span,) = _value_spans(key, value, [Span(low, high, "X", "")])
            assert (span.start, span.end, span.text) == (i, i + 1, char)
    # Cut to the value: the key and the quotes are not part of it.
    (span,) = _value_spans(key, value, [Span(0, len(_probe(key, value)), "X", "")])
    assert (span.start, span.end) == (0, len(value))


def test_keeps_the_protocol_version_and_context_in_the_key(tmp_path: Path) -> None:
    """With a date pattern and `context` words, as a config may have them."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "detector:\n"
        "  type: regex\n"
        "  patterns:\n"
        "    DATE: '\\d{4}-\\d{2}-\\d{2}'\n"
        "    EMPLOYEE_ID: '^EMP\\d{4}$'\n"
        "  context:\n"
        "    EMAIL: [owner]\n",
        encoding="utf-8",
    )
    proc = _wrap(config, tmp_path / "server.log")
    assert proc.stdout is not None
    try:
        _send(proc, "context")
        result = json.loads(proc.stdout.readline())["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert result["structuredContent"] == {
            # The context word is the key: the email is found in part.
            "owner": "contact <PRIVYX_EMAIL_1>",
            "other": "bob@example.com",
            # Found alone only: in `employee: "EMP1234"` the anchors fail.
            "employee": "<PRIVYX_EMPLOYEE_ID_2>",
        }
    finally:
        proc.kill()


def test_masks_values_in_a_list_by_the_key_it_is_under(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "secret-lists")
        content = json.loads(proc.stdout.readline())["result"]["structuredContent"]
        assert content == {
            "passwords": ["<PRIVYX_SECRET_1>", "<PRIVYX_SECRET_2>"],
            "password": [["<PRIVYX_SECRET_3>"]],
        }
        _send(proc, "tools/call", arguments=content)
        proc.stdout.readline()
        assert _received(log)[1]["params"]["arguments"] == {
            "passwords": ["hunter2", "letmein"],
            "password": [["s3cret"]],
        }
    finally:
        proc.kill()


@pytest.mark.parametrize("direction", ["mask", "restore"])
def test_stops_when_the_session_vanishes(config: Path, tmp_path: Path, direction: str) -> None:
    """A session made again would give the client's old tokens to new values."""
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    async def delete() -> None:
        engine, close = await build_engine(load_config(config))
        try:
            assert await engine.delete_session("s1", reason="test")
        finally:
            await close()

    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        _send(proc, "tools/call")
        proc.stdout.readline()
        if direction == "mask":
            _send(proc, "later")  # answered after the session is gone
            asyncio.run(delete())
        else:
            asyncio.run(delete())
            _send(proc, "notify", to="<PRIVYX_EMAIL_1>")
        assert proc.wait(timeout=10) == 1
        assert proc.stdout.read() == b""
        assert b"session not found: s1" in proc.stderr.read()
        assert len(_received(log)) == (2 if direction == "mask" else 1)
    finally:
        proc.kill()


def test_sigterm_stops_a_server_whose_client_stopped_reading(config: Path, tmp_path: Path) -> None:
    """A full stdout must not hold up the SIGTERM handler."""
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    try:
        _send(proc, "big")  # more than a pipe holds, and never read
        _wait_for_server(log)
        time.sleep(1)  # the reply is being written
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        proc.kill()


@pytest.mark.parametrize("stop", ["mask-error", "sigterm"])
def test_stops_with_unread_server_output(config: Path, tmp_path: Path, stop: str) -> None:
    """Waiting for the server must not wait on output it is no longer reading.

    The read limit is made small so the backlog fills the buffer, as 128 MiB
    of unread output would.
    """
    script = "import privyx.cli.commands.mcp as m\nm._LINE_LIMIT = 1024\n"
    if stop == "mask-error":
        script += (
            "import asyncio, privyx.core.engine as e\n"
            "async def boom(*a, **k):\n"
            "    await asyncio.sleep(1)\n"
            "    raise OSError('boom')\n"
            "e.PrivacyEngine.transform = boom\n"
        )
    script += "from privyx.cli.main import cli; cli()\n"
    log = tmp_path / "server.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "mcp", "wrap", "-c", str(config), "--"]
        + [sys.executable, "-c", SERVER, str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _send(proc, "flood")  # never read by this client
        if stop == "sigterm":
            _wait_for_server(log)
            time.sleep(1)  # the backlog fills the buffer
            proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == (1 if stop == "mask-error" else 128 + signal.SIGTERM)
    finally:
        proc.kill()


def test_keeps_the_ids_notifications_point_with(config: Path, tmp_path: Path) -> None:
    """A cancel or progress must still match the request the client sent."""
    proc = _wrap(config, tmp_path / "server.log")
    assert proc.stdout is not None
    try:
        _send(proc, "notes")
        cancelled, progress, request = (json.loads(proc.stdout.readline()) for _ in range(3))
        assert cancelled["params"] == {"requestId": "ana@example.com", "reason": "<PRIVYX_EMAIL_1>"}
        assert progress["params"]["progressToken"] == "ana@example.com"
        assert request["params"] == {"_meta": {"progressToken": "ana@example.com"}}
    finally:
        proc.kill()


def test_kills_a_server_that_ignores_sigterm(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "stubborn")
        proc.stdout.readline()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGKILL
        _gone(int((tmp_path / "server.log.pid").read_text()))
    finally:
        proc.kill()


def test_kills_what_the_server_left_behind(config: Path, tmp_path: Path) -> None:
    """A child that ignores SIGTERM and holds no pipe outlives the server."""
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "orphan")
        proc.stdout.readline()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
        _gone(int((tmp_path / "server.log.child").read_text()))
    finally:
        proc.kill()


def test_shares_the_session_with_another_writer(config: Path, tmp_path: Path) -> None:
    """Values another writer masked meanwhile keep their tokens, both ways."""
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        time.sleep(1.5)  # past start-up
        other = CliRunner().invoke(
            cli, ["pipe", "-c", str(config), "--session", "s1"], input="alice@example.com\n"
        )
        assert other.stdout == "<PRIVYX_EMAIL_1>\n"
        _send(proc, "tools/call", arguments={"to": "<PRIVYX_EMAIL_1>"})
        assert "<PRIVYX_EMAIL_2>" in proc.stdout.readline().decode()
        assert _received(log)[0]["params"]["arguments"]["to"] == "alice@example.com"
    finally:
        proc.kill()


def test_sends_only_what_it_checked_and_masks_plain_output(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None
    try:
        _send(proc, "raw")
        line = proc.stdout.readline()
        assert b"ana@example.com" not in line
        assert json.loads(line)["result"] == {"content": [{"type": "text", "text": "ok"}]}
        _send(proc, "log")
        assert proc.stdout.readline() == b"plain log from <PRIVYX_EMAIL_1>\n"
        # JSON that is not JSON-RPC is content all the same.
        assert json.loads(proc.stdout.readline()) == {"level": "info", "msg": "<PRIVYX_EMAIL_1>"}
        # Without "jsonrpc" not even "method" is protocol; keys are content too.
        assert json.loads(proc.stdout.readline()) == {
            "method": "<PRIVYX_EMAIL_1>",
            "<PRIVYX_EMAIL_1>": 1,
        }
    finally:
        proc.kill()


def test_exits_with_the_server(config: Path, tmp_path: Path) -> None:
    """The client's stdin stays open: wrap must not wait for it."""
    proc = _wrap(config, tmp_path / "server.log")
    try:
        _send(proc, "exit")
        assert proc.wait(timeout=10) == 3
    finally:
        proc.kill()


def test_sigterm_stops_the_server(config: Path, tmp_path: Path) -> None:
    proc = _wrap(config, tmp_path / "server.log")
    assert proc.stdout is not None
    try:
        _send(proc, "tools/call")
        proc.stdout.readline()  # started
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        proc.kill()


def test_sigterm_stops_a_server_run_by_a_launcher(config: Path, tmp_path: Path) -> None:
    """As under npx: the server is the launcher's child, holding wrap's pipes."""
    log = tmp_path / "server.log"
    launcher = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"
    server = (sys.executable, "-c", SERVER, str(log))
    proc = _wrap(config, log, sys.executable, "-c", launcher, *server)
    assert proc.stdout is not None
    try:
        _send(proc, "tools/call")
        proc.stdout.readline()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
        _gone(int((tmp_path / "server.log.pid").read_text()))
    finally:
        proc.kill()


def test_ctrl_c_stops_an_idle_server(config: Path, tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        _send(proc, "tools/call")
        proc.stdout.readline()
        proc.send_signal(signal.SIGINT)  # to wrap alone; the server waits for input
        assert proc.wait(timeout=10) == 130
        assert b"Error" not in proc.stderr.read()
        _gone(int((tmp_path / "server.log.pid").read_text()))
    finally:
        proc.kill()


def _gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        assert time.monotonic() < deadline, f"server {pid} still runs"
        time.sleep(0.1)


def test_a_reply_that_fails_to_mask_is_not_sent(config: Path, tmp_path: Path) -> None:
    script = (
        "import privyx.core.engine as e\n"
        "async def boom(*a, **k): raise OSError('boom')\n"
        "e.PrivacyEngine.transform = boom\n"
        "from privyx.cli.main import cli; cli()\n"
    )
    log = tmp_path / "server.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "mcp", "wrap", "-c", str(config), "--"]
        + [sys.executable, "-c", SERVER, str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _send(proc, "tools/call")
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 1
        assert out == b""
        assert b"Error: boom" in err
    finally:
        proc.kill()


@pytest.mark.parametrize(
    ("operator", "message"),
    [
        # Mask: redact gives both emails the same text.
        ("redact", {"ana@example.com": {"balance": 10}, "bob@example.com": {"balance": 20}}),
        # Mask: a key that already reads like the token the other is given.
        ("pseudonym", {"ana@example.com": 1, "<PRIVYX_EMAIL_1>": 2}),
    ],
)
async def test_keys_made_one_are_an_error(operator: str, message: dict[str, Any]) -> None:
    from privyx.cli.commands.mcp import _rewrite
    from privyx.config.schema import Settings
    from privyx.core.builder import build_engine
    from privyx.core.errors import OperatorError

    settings = Settings.model_validate(
        {"detector": {"type": "regex"}, "operator": {"type": operator}}
    )
    engine, close = await build_engine(settings)
    try:
        session = await engine.get_or_create_session("x")

        async def mask(text: str) -> str:
            return (await engine.transform(text, session=session, persist=False)).text

        line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": message}).encode()
        with pytest.raises(OperatorError, match="two keys of one object"):
            await _rewrite(line, mask)
    finally:
        await close()


def test_restored_keys_made_one_stop_the_run(config: Path, tmp_path: Path) -> None:
    """Restore: a literal key and a token that restores to it."""
    log = tmp_path / "server.log"
    proc = _wrap(config, log)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        _send(proc, "tools/call")
        proc.stdout.readline()  # bob@example.com is <PRIVYX_EMAIL_1> now
        _send(proc, "tools/call", arguments={"bob@example.com": 1, "<PRIVYX_EMAIL_1>": 2})
        assert proc.wait(timeout=10) == 1
        assert proc.stdout.read() == b""
        assert b"two keys of one object" in proc.stderr.read()
        assert len(_received(log)) == 1
    finally:
        proc.kill()


def test_missing_command_fails(config: Path) -> None:
    result = CliRunner().invoke(cli, ["mcp", "wrap", "-c", str(config), "--", "no-such-cmd-x"])
    assert result.exit_code == 1
    assert "cannot run no-such-cmd-x" in result.output
