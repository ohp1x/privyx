"""Exercise exec, terminal job control and proxy cleanup through a real shell."""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import signal
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX controlling terminal required")

_AGENT = """
import json, os, signal, sys, urllib.request
base = os.environ['ANTHROPIC_BASE_URL']
def health():
    with urllib.request.urlopen(base + '/health', timeout=5) as response:
        return response.status
def interrupt(*_):
    print('AGENT_INTERRUPT=' + str(health()), flush=True)
    sys.exit(42)
signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGCONT, lambda *_: print('AGENT_CONTINUED', flush=True))
print('AGENT_READY=' + json.dumps({
    'pid': os.getpid(), 'parent': os.getppid(), 'foreground': os.tcgetpgrp(0), 'base': base,
    'args': sys.argv[1:], 'flag': os.environ['PROFILE_FLAG'],
    'reference': os.environ['PROFILE_REFERENCE'], 'health': health(),
}), flush=True)
for line in sys.stdin:
    if line.strip() == 'quit':
        sys.exit(7)
    if line.strip() == 'health':
        print('AGENT_HEALTH=' + str(health()), flush=True)
    if line.strip() == 'request':
        request = urllib.request.Request(base + '/v1/messages',
            data=json.dumps({'model': 'test', 'messages': [
                {'role': 'user', 'content': 'alice@example.com'}
            ]}).encode(), headers={'content-type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            print('AGENT_REPLY=' + response.read().decode(), flush=True)
"""


class Shell:
    def __init__(self, tmp_path: Path) -> None:
        import pty

        self.buffer = b""
        self.offset = 0
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(tmp_path)
            env = {key: value for key, value in os.environ.items() if not key.startswith("PRIVYX_")}
            env.update(TERM="xterm-256color", PS1="privyx-test$ ", WORK_VALUE="from-shell")
            os.execve("/bin/sh", ["sh", "-i"], env)

    def send(self, command: str) -> None:
        os.write(self.fd, command.encode())

    def expect(self, pattern: bytes, timeout: float = 15) -> re.Match[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, self.buffer[self.offset :])
            if match:
                self.offset += match.end()
                return match
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.buffer += data
        pytest.fail(f"did not see {pattern!r} in terminal output: {self.buffer!r}")

    def launch(
        self, config: Path, agent: str, tmp_path: Path, *, port: int = 0, via_uv: bool = False
    ) -> None:
        launcher_pid = tmp_path / "launcher.pid"
        code = (
            "import os; from pathlib import Path; "
            f"Path({str(launcher_pid)!r}).write_text(str(os.getpid())); "
            "from privyx.cli.main import cli; cli()"
        )
        if via_uv:
            entrypoint = [
                "uv",
                "run",
                "--project",
                str(Path(__file__).resolve().parents[2]),
                "privyx",
            ]
        else:
            entrypoint = [sys.executable, "-c", code]
        command = shlex.join(
            [
                *entrypoint,
                "run",
                "--no-anchor",
                "--port",
                str(port),
                "-c",
                str(config),
                agent,
                "--",
                "--value",
                "with spaces",
            ]
        )
        self.send(command + '; printf "\\nPRIVYX_EXIT:%s\\n" "$?"\n')

    def ready(self) -> dict[str, Any]:
        return json.loads(self.expect(rb"AGENT_READY=([^\r\n]+)\r?\n")[1])  # type: ignore[no-any-return]

    def exit_code(self) -> int:
        return int(self.expect(rb"\r?\nPRIVYX_EXIT:(\d+)\r?\n")[1])

    def close(self) -> None:
        try:
            foreground = os.tcgetpgrp(self.fd)
            if foreground > 0 and foreground != os.getpgrp():
                os.killpg(foreground, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)


@pytest.fixture
def shell(tmp_path: Path) -> Iterator[Shell]:
    session = Shell(tmp_path)
    try:
        yield session
    finally:
        session.close()


def _config(
    tmp_path: Path, agent: str, *, command: str = sys.executable, upstream: str = ""
) -> Path:
    probe = tmp_path / "agent.py"
    probe.write_text(_AGENT)
    config = tmp_path / "native.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    agent: {
                        "command": command,
                        "adapter": "env",
                        "provider": "anthropic",
                        "base_path": "",
                        "env_vars": ["ANTHROPIC_BASE_URL"],
                        "args": [str(probe)],
                        "env": {"PROFILE_FLAG": "from-profile"},
                        "env_from": {"PROFILE_REFERENCE": "WORK_VALUE"},
                    }
                },
                "audit": {"path": str(tmp_path / "audit.log")},
                "upstream_url": upstream,
            }
        )
    )
    return config


def _assert_proxy_closed(base: str) -> None:
    from urllib.parse import urlsplit

    address = urlsplit(base)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (address.hostname or "", address.port or 80), timeout=0.1
            ):
                pass
        except OSError:
            return
        time.sleep(0.05)
    pytest.fail(f"proxy still listening after agent exit: {base}")


@pytest.mark.parametrize("agent", ["codex", "claude", "aider", "pi", "work"])
def test_interactive_agent_replaces_launcher_and_preserves_launch_plan(
    shell: Shell, tmp_path: Path, agent: str
) -> None:
    shell.launch(_config(tmp_path, agent), agent, tmp_path)
    info = shell.ready()
    assert info["pid"] == int((tmp_path / "launcher.pid").read_text())
    assert info["foreground"] == info["pid"]
    assert info["args"] == ["--value", "with spaces"]
    assert info["flag"] == "from-profile" and info["reference"] == "from-shell"
    assert info["health"] == 200
    assert f"\033]0;{agent}\007".encode() in shell.buffer
    shell.send("quit\n")
    assert shell.exit_code() == 7
    _assert_proxy_closed(info["base"])


def test_uv_run_announces_agent_title_and_execs_without_python_supervisor(
    shell: Shell, tmp_path: Path
) -> None:
    import shutil

    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    shell.launch(_config(tmp_path, "codex"), "codex", tmp_path, via_uv=True)
    info = shell.ready()
    # uv keeps the foreground group, but Privyx itself must be replaced by the
    # agent. VS Code needs tabs.title=${sequence} to use the emitted OSC title.
    assert info["parent"] == info["foreground"]
    assert b"\033]0;codex\007" in shell.buffer
    assert info["args"] == ["--value", "with spaces"]
    shell.send("quit\n")
    assert shell.exit_code() == 7
    _assert_proxy_closed(info["base"])


def test_ctrl_c_reaches_agent_while_proxy_stays_alive(shell: Shell, tmp_path: Path) -> None:
    shell.launch(_config(tmp_path, "codex"), "codex", tmp_path)
    info = shell.ready()
    shell.send("\x03")
    shell.expect(rb"AGENT_INTERRUPT=200")
    assert shell.exit_code() == 42
    _assert_proxy_closed(info["base"])


def test_ctrl_z_and_fg_preserve_agent_and_proxy(shell: Shell, tmp_path: Path) -> None:
    shell.launch(_config(tmp_path, "codex"), "codex", tmp_path)
    info = shell.ready()
    shell.send("\x1a")
    shell.expect(rb"Stopped|stopped|Suspended")
    assert httpx.get(info["base"] + "/health").status_code == 200
    shell.send("fg\n")
    shell.expect(rb"AGENT_CONTINUED")
    shell.send("health\n")
    shell.expect(rb"AGENT_HEALTH=200")
    shell.send("quit\n")
    shell.expect(rb"privyx-test\$ ")
    # The original command's printf ran when it stopped; collect fg's status.
    shell.send('printf "\\nPRIVYX_EXIT:%s\\n" "$?"\n')
    assert shell.exit_code() == 7
    _assert_proxy_closed(info["base"])


def test_sigkill_of_agent_stops_proxy(shell: Shell, tmp_path: Path) -> None:
    shell.launch(_config(tmp_path, "codex"), "codex", tmp_path)
    info = shell.ready()
    os.kill(info["pid"], signal.SIGKILL)
    assert shell.exit_code() == 128 + signal.SIGKILL
    _assert_proxy_closed(info["base"])


def test_failed_exec_cleans_up_started_proxy(shell: Shell, tmp_path: Path) -> None:
    tool = tmp_path / "broken-agent"
    tool.write_text("#!/missing/agent-interpreter\n")
    tool.chmod(0o700)
    shell.launch(_config(tmp_path, "work", command=str(tool)), "work", tmp_path)
    base = shell.expect(rb"ANTHROPIC_BASE_URL = (http://[^\r\n]+)")[1].decode()
    shell.expect(rb"Error: cannot launch")
    assert shell.exit_code() == 1
    _assert_proxy_closed(base)


def test_failed_proxy_startup_does_not_exec_agent(shell: Shell, tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        shell.launch(_config(tmp_path, "codex"), "codex", tmp_path, port=listener.getsockname()[1])
        shell.expect(rb"Error: cannot listen")
        assert shell.exit_code() == 1
        assert b"AGENT_READY=" not in shell.buffer


def test_native_agent_traffic_is_masked_and_restored(shell: Shell, tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict[str, Any]] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append(body)
            reply = json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": body["messages"][0]["content"]}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config(tmp_path, "codex", upstream=f"http://127.0.0.1:{server.server_port}")
        shell.launch(config, "codex", tmp_path)
        info = shell.ready()
        shell.send("request\n")
        reply = json.loads(shell.expect(rb"AGENT_REPLY=([^\r\n]+)\r?\n")[1])
        assert reply["content"][0]["text"] == "alice@example.com"
        assert len(seen) == 1
        assert seen[0]["messages"][0]["content"].startswith("<PRIVYX_EMAIL_")
        assert "alice@example.com" not in json.dumps(seen)
        shell.send("quit\n")
        assert shell.exit_code() == 7
        _assert_proxy_closed(info["base"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
