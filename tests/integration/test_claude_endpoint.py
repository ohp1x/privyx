"""An installed Claude keeps its native destination behind the masking proxy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "source",
    [
        "shell",
        "env",
        "env_from",
        "user",
        "project",
        "local",
        "settings",
        "managed",
        "subdir-project",
        "subdir-local",
    ],
)
def test_real_claude_native_endpoint_is_masked(tmp_path: Path, source: str) -> None:
    claude = shutil.which("claude")
    if not claude:
        pytest.skip("Claude Code is not installed")
    prefix: list[str] = []
    if source == "managed":
        unshare = shutil.which("unshare")
        if (
            not unshare
            or subprocess.run(
                [unshare, "--user", "--map-root-user", "--mount", "true"], capture_output=True
            ).returncode
        ):
            pytest.skip("managed settings test requires mount namespaces")
        etc = tmp_path / "etc"
        (etc / "claude-code").mkdir(parents=True)
        prefix = [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "sh",
            "-c",
            'mount --bind "$1" /etc || exit; shift; exec "$@"',
            "sh",
            str(etc),
        ]
    seen: list[tuple[str, str, str | None]] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["content-length"])).decode()
            seen.append((self.path, raw, self.headers.get("authorization")))
            tokens = re.findall(r"<PRIVYX_EMAIL_[A-Za-z0-9]+>", raw)
            text = "Echo: " + (tokens[0] if tokens else "unexpected direct request")
            message = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            events = [
                {"type": "message_start", "message": message},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
            if self.path.split("?")[0].endswith("/count_tokens"):
                data, mime = b'{"input_tokens":1}', "application/json"
            else:
                data = "".join(
                    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                ).encode()
                mime = "text/event-stream"
            self.send_response(200)
            self.send_header("content-type", mime)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    native = tmp_path / "native"
    native.mkdir()
    project = tmp_path / ".claude"
    project.mkdir()
    cwd = tmp_path
    if source.startswith("subdir-"):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        filename = "settings.json" if source == "subdir-project" else "settings.local.json"
        (project / filename).write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": base
                        if source == "subdir-local"
                        else "http://127.0.0.1:9"
                    }
                }
            )
        )
        cwd = tmp_path / "child"
        project = cwd / ".claude"
        project.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ANTHROPIC_", "CLAUDE", "PRIVYX_", "OPENAI_", "CODEX_"))
    }
    env.update(
        CLAUDE_CONFIG_DIR=str(native),
        ANTHROPIC_AUTH_TOKEN="dummy-router-credential",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    profile: dict[str, object] = {"command": claude}
    args = []
    if source in ("shell", "settings"):
        env["ANTHROPIC_BASE_URL"] = base
        if source == "settings":
            args = ["--settings", '{"model":"test"}']
    elif source == "env":
        profile["env"] = {"ANTHROPIC_BASE_URL": base}
    elif source == "env_from":
        env["ROUTER_URL"] = base
        profile["env_from"] = {"ANTHROPIC_BASE_URL": "ROUTER_URL"}
    else:
        path = {
            "user": native / "settings.json",
            "project": project / "settings.json",
            "local": project / "settings.local.json",
            "managed": tmp_path / "etc/claude-code/managed-settings.json",
            "subdir-project": project / "settings.json",
            "subdir-local": project / "settings.local.json",
        }[source]
        path.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"
                        if source == "subdir-local"
                        else base
                    }
                }
            )
        )
    config = tmp_path / "privyx.yaml"
    config.write_text(
        yaml.safe_dump(
            {"agents": {"claude": profile}, "audit": {"path": str(tmp_path / "audit.log")}}
        )
    )
    try:
        if source.startswith("subdir-"):
            direct = subprocess.run(
                [claude, "-p", "Reply with alice@example.com.", "--model", "test", "--tools", ""],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert direct.returncode == 0, direct.stdout + direct.stderr
            assert seen and "alice@example.com" in json.dumps(seen)
            seen.clear()
        if source == "managed":
            native_result = subprocess.run(
                [
                    *prefix,
                    claude,
                    "-p",
                    "Reply with alice@example.com.",
                    "--model",
                    "test",
                    "--tools",
                    "",
                    "--settings",
                    '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:9"}}',
                ],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert native_result.returncode == 0, native_result.stdout + native_result.stderr
            assert seen and "alice@example.com" in json.dumps(seen)
            seen.clear()
        result = subprocess.run(
            [
                *prefix,
                sys.executable,
                "-c",
                "from privyx.cli.main import cli; cli()",
                "run",
                "--no-anchor",
                "-c",
                str(config),
                "claude",
                "--",
                "-p",
                "Reply with alice@example.com.",
                "--model",
                "test",
                "--max-turns",
                "1",
                "--tools",
                "",
                *args,
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if source == "managed":
            assert result.returncode == 1, result.stdout + result.stderr
            assert "managed settings define ANTHROPIC_BASE_URL" in result.stderr
            assert not seen
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert seen
        assert all(path.startswith("/v1/messages") for path, _, _ in seen)
        assert "alice@example.com" not in json.dumps(seen)
        assert all(auth == "Bearer dummy-router-credential" for _, _, auth in seen)
        assert "Echo: alice@example.com" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
