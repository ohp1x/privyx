"""An installed Codex must not bypass Privyx when using its own custom provider."""

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
from typing import Any

import pytest
import yaml


def test_real_codex_custom_provider_is_masked_and_keeps_its_auth(tmp_path: Path) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("codex CLI is not installed")
    seen: list[tuple[str, dict[str, Any], str | None]] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.path, body, self.headers.get("authorization")))
            tokens = re.findall(r"<PRIVYX_EMAIL_[A-Za-z0-9]+>", json.dumps(body))
            text = "Echo: " + (tokens[0] if tokens else "unexpected direct request")
            item = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            response = {
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": body.get("model", "test"),
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            common = {"item_id": item["id"], "output_index": 0, "content_index": 0}
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "content": []},
                },
                {
                    "type": "response.content_part.added",
                    **common,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
                {"type": "response.output_text.delta", **common, "delta": text},
                {"type": "response.output_text.done", **common, "text": text},
                {"type": "response.content_part.done", **common, "part": item["content"][0]},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            data = "".join(
                f"event: {event['type']}\n"
                f"data: {json.dumps({**event, 'sequence_number': index})}\n\n"
                for index, event in enumerate(events)
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
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
    root = tmp_path / "codex"
    root.mkdir()
    original = (
        'model_provider="9router"\ncheck_for_update_on_startup=false\n'
        '[model_providers.9router]\nname="Router"\nwire_api="responses"\n'
        f'base_url="{base}/bypass"\nrequires_openai_auth=false\n'
        'http_headers={Authorization="Bearer dummy-router-credential"}\n'
    )
    (root / "config.toml").write_text(original)
    config = tmp_path / "privyx.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "upstream_url": base,
                "agents": {"codex": {"command": codex}},
                "audit": {"path": str(tmp_path / "audit.log")},
            }
        )
    )
    # No real credentials, provider endpoints, user config or project instructions.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "CODEX_", "PRIVYX_", "ANTHROPIC_"))
    }
    env["CODEX_HOME"] = str(root)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from privyx.cli.main import cli; cli()",
                "run",
                "--no-anchor",
                "-c",
                str(config),
                "codex",
                "--",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                "test",
                "Reply with alice@example.com.",
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert seen
        assert all(path == "/v1/responses" for path, _, _ in seen)
        assert "alice@example.com" not in json.dumps([body for _, body, _ in seen])
        assert all(auth == "Bearer dummy-router-credential" for _, _, auth in seen)
        assert "Echo: alice@example.com" in result.stdout
        assert "no request from codex" not in result.stderr
        assert (root / "config.toml").read_text() == original
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
