"""End-to-end leak check: real Claude Code → Privyx → fake Anthropic.

    uv run python scripts/e2e_claude.py                 # `privyx run claude`
    uv run python scripts/e2e_claude.py --transparent   # `privyx proxy`, its defaults

Everything lives in a fresh temp dir — HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME
(so the anchor key too), the workspace, the logs — so the real ~/.claude and
~/.config/privyx are never touched, and no real credential or network is used.

Canary values are seeded wherever Claude Code picks up context: the prompt,
CLAUDE.md, the workspace path, git branch/log, an MCP server's instructions,
tool description, schema and output, and a file read through Bash.  The fake
upstream scripts one agent loop (thinking + MCP call → thinking + Bash call →
text echoing every token it saw) and records every request Privyx forwards.
It fails on:

- a canary in a recorded request — a leak;
- a token in the MCP tool's arguments or Claude's final answer — a missed restore;
- an echoed assistant turn that differs from what the upstream sent — the
  provider rejects a modified thinking block (its signature covers the text);
- ``system`` / ``tools`` changing between turns — every turn misses the prompt cache.

The temp dir is kept for inspection.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PERSON = "Zorblatt Quandry"
GIT_USER = "Quinta Vexley"  # Claude Code reports it in gitStatus ("Git user: ...")
ORG = "Canarycorp"
EMAIL = "zorblatt.quandry@canary.test"
IP = "10.20.30.40"
CANARIES = (PERSON, GIT_USER, ORG, EMAIL, IP)
# Not a canary: the model's own output, which must come back byte-identical.
MODEL_EMAIL = "support@vendor.test"
TOKEN = re.compile(r"<PRIVYX_[^<>\s]+>")
MCP_TOOL = "mcp__canary__lookup"
PROMPT = f"Look up {PERSON} at {ORG} ({EMAIL}) and summarize."
CONFIG = f"""\
detector:
  type: regex
  terms:
    PERSON: ["{PERSON}", "{GIT_USER}"]
    ORGANIZATION: ["{ORG}"]
"""

RECORDS: list[dict[str, Any]] = []
SENT: list[list[dict[str, Any]]] = []  # assistant turns the upstream sent in the main loop


def mcp_server(log: str) -> None:
    """Minimal stdio MCP server whose metadata and output carry canaries."""
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notification
        method, result = msg.get("method"), None
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "canary", "version": "1"},
                "instructions": f"Records belong to {ORG}; escalate to {EMAIL}.",
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": f"Look up a {ORG} customer such as {PERSON}.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string", "description": EMAIL}},
                            "required": ["query"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            with open(log, "a") as f:
                f.write(json.dumps(msg["params"]) + "\n")
            text = f"{PERSON} <{EMAIL}>, {ORG}, last login from {IP}"
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": f"unsupported: {method}"}
        else:
            reply["result"] = result
        print(json.dumps(reply), flush=True)


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


class Upstream(BaseHTTPRequestHandler):
    """Fake Anthropic API: records each forwarded request, replies from ``script``."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.loads(raw or b"{}")
        RECORDS.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path.endswith("/count_tokens"):
            self._send(json.dumps({"input_tokens": 1}).encode(), "application/json")
            return
        blocks = script(body)
        stop = "tool_use" if blocks[-1]["type"] == "tool_use" else "end_turn"
        if body.get("stream"):
            self._send(sse(blocks, stop), "text/event-stream")
        else:
            self._send(json.dumps(message(blocks, stop)).encode(), "application/json")

    def do_GET(self) -> None:  # a transparent proxy forwards any path
        RECORDS.append({"path": self.path, "headers": dict(self.headers), "body": None})
        self._send(b'{"type": "error"}', "application/json", 404)

    def _send(self, out: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_: Any) -> None:
        pass


def leaks(obj: Any, path: str = "$") -> Iterator[tuple[str, str, str]]:
    """Yield ``(json_path, canary, context)`` for every canary in ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaks(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaks(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        for canary in CANARIES:
            at = obj.lower().find(canary.lower())
            if at >= 0:
                yield path, canary, obj[max(0, at - 30) : at + len(canary) + 30]


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


def drift(bodies: list[dict[str, Any]], key: str) -> str:
    """The first difference in ``key`` between consecutive turns, or ``""``."""
    for a, b in zip(bodies, bodies[1:], strict=False):
        x, y = json.dumps(a.get(key)), json.dumps(b.get(key))
        if x != y:
            pairs = enumerate(zip(x, y, strict=False))
            at = next((i for i, (p, q) in pairs if p != q), min(len(x), len(y)))
            return f"{x[max(0, at - 30) : at + 30]!r}\n      → {y[max(0, at - 30) : at + 30]!r}"
    return ""


def seed(root: Path, env: dict[str, str]) -> Path:
    """Create the canary-laden workspace, Claude/MCP/Privyx config; return the workspace."""
    work = root / f"{ORG.lower()}-app"  # the path lands in Claude's environment section
    work.mkdir()
    (root / "home" / ".claude").mkdir(parents=True)
    (work / "CLAUDE.md").write_text(f"# Project\n\nOwner: {PERSON} <{EMAIL}> at {ORG}.\n")
    (work / "notes.txt").write_text(f"{ORG} VPN gateway {IP}, contact {EMAIL}\n")
    git = ["git", "-C", str(work)]
    for args in (
        ["init", "-q", "-b", f"{ORG.lower()}-main"],
        ["config", "user.name", GIT_USER],
        ["config", "user.email", EMAIL],
        ["add", "."],
        ["commit", "-qm", f"Onboard {PERSON} to {ORG}"],
    ):
        subprocess.run([*git, *args], env=env, check=True)
    (root / "privyx.yaml").write_text(CONFIG)
    server = {
        "command": sys.executable,
        "args": [os.path.abspath(__file__), "--mcp", str(root / "mcp-calls.jsonl")],
    }
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"canary": server}}))
    return work


def start_proxy(
    root: Path, env: dict[str, str], upstream: str
) -> tuple[subprocess.Popen[Any], str]:
    """Start ``privyx proxy --transparent`` with its defaults; return it and its URL."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cmd = ["privyx", "proxy", "--transparent", "-c", str(root / "privyx.yaml"),
           "--port", str(port), "--upstream", upstream]  # fmt: skip
    log = open(root / "proxy.log", "w")  # noqa: SIM115 - owned by the child
    proc = subprocess.Popen(cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=1)
            return proc, url
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit(f"privyx proxy did not start; see {root / 'proxy.log'}")


def main(transparent: bool) -> int:
    root = Path(tempfile.mkdtemp(prefix="privyx-e2e-"))
    home = root / "home"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "CLAUDE", "PRIVYX_", "XDG_CONFIG_HOME", "GIT_"))
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        ANTHROPIC_API_KEY="sk-ant-e2e-dummy",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        GIT_CONFIG_NOSYSTEM="1",
        PRIVYX_AUDIT_PATH=str(root / "audit.log"),
    )
    work = seed(root, env)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{server.server_address[1]}"

    # Our fake upstream scripts every tool call, all inside the temp workspace.
    claude = ["-p", PROMPT, "--mcp-config", str(root / "mcp.json"), "--strict-mcp-config",
              "--dangerously-skip-permissions"]  # fmt: skip
    proxy = None
    if transparent:
        proxy, env["ANTHROPIC_BASE_URL"] = start_proxy(root, env, upstream)
        cmd = ["claude", *claude]
    else:
        # A base URL in Claude Code's own settings outranks its environment;
        # privyx run must reach it anyway (nothing answers on port 9).
        settings = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}
        (home / ".claude" / "settings.json").write_text(json.dumps(settings))
        cmd = ["privyx", "run", "claude", "-c", str(root / "privyx.yaml"),
               "--upstream", f"{upstream}/v1/messages", "--", *claude]  # fmt: skip
    proc = subprocess.Popen(
        cmd,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out = (proc.communicate()[0] or "") + "\n[e2e] timed out after 180s\n"
    if proxy is not None:
        proxy.terminate()
        proxy.wait()
    server.shutdown()

    (root / "claude.out").write_text(out)
    with open(root / "upstream.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in RECORDS)

    failed = False
    found: dict[tuple[str, str], tuple[list[int], str]] = {}
    for n, record in enumerate(RECORDS, 1):
        for path, canary, context in leaks(record):
            found.setdefault((path, canary), ([], context))[0].append(n)
    paths = ", ".join(dict.fromkeys(r["path"] for r in RECORDS))
    mode = "privyx proxy --transparent" if transparent else "privyx run"
    print(f"{mode}: upstream saw {len(RECORDS)} request(s) [{paths}]; exit {proc.returncode}")
    for (path, canary), (reqs, context) in found.items():
        failed = True
        print(f"LEAK  {canary!r} at {path}  (req {','.join(map(str, reqs))})\n      …{context!r}…")

    def check(ok: bool, what: str, detail: str) -> None:
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what}: {detail}")

    calls = root / "mcp-calls.jsonl"
    args = calls.read_text().strip() if calls.exists() else ""
    check(bool(args) and not TOKEN.search(args), "MCP tool args restored", args or "(never called)")
    echo = next((ln for ln in out.splitlines() if ln.startswith("Echo:")), "")
    check(bool(echo) and not TOKEN.search(echo), "final answer restored", echo or "(no Echo line)")

    main_records = [r for r in RECORDS if r["body"] and is_main(r["body"])]
    keys = {r["headers"].get("x-api-key") for r in main_records}
    check(keys == {env["ANTHROPIC_API_KEY"]}, "client key relayed", f"{len(main_records)} turn(s)")

    bodies = [r["body"] for r in main_records]
    problems = roundtrip(bodies)
    check(not problems, "echoed turns byte-identical", f"{len(SENT)} turn(s) sent")
    for problem in problems:
        print(f"      {problem}")
    for key in ("system", "tools"):
        moved = drift(bodies, key)
        check(not moved, f"{key} stable across turns", moved or f"{len(bodies)} turn(s)")

    print(f"artifacts: {root}  (upstream.jsonl, claude.out, audit.log, mcp-calls.jsonl)")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mcp"]:
        mcp_server(sys.argv[2])
    else:
        sys.exit(main(transparent="--transparent" in sys.argv[1:]))
