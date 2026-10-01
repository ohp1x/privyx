"""End-to-end leak check: a real agent → Privyx → a fake or real provider.

    uv run python scripts/test-e2e/run.py                 # `privyx run claude`
    uv run python scripts/test-e2e/run.py --transparent   # `privyx proxy`, its defaults
    uv run python scripts/test-e2e/run.py --upstream https://api.anthropic.com
    uv run python scripts/test-e2e/run.py --agent codex --upstream https://api.openai.com

Each agent has its own file next to this one (claude.py, codex.py) and is an
``Agent`` from common.py; this file is the run they share.

Everything lives in a fresh temp dir — HOME, the agent's config dir,
XDG_CONFIG_HOME (so the anchor key too), the workspace, the logs — so the real
~/.claude, ~/.codex and ~/.config/privyx are never touched.  Without
``--upstream`` no real credential or network is used.

Canary values are seeded wherever the agent picks up context: the prompt,
CLAUDE.md / AGENTS.md, the workspace path, git branch/log, an MCP server's
instructions, tool description, schema and output, and a file the agent reads.
The agent's fake upstream scripts one agent loop (thinking + MCP call → thinking
+ Bash call → text echoing every token it saw) and every request Privyx
forwards is recorded.  The run fails on:

- a canary in a recorded request — a leak;
- a token in the MCP tool's arguments or the final answer, with or without its
  ``< >`` or as its id alone — a missed restore;
- an echoed assistant turn that differs from what the upstream sent — the
  provider rejects a modified thinking block (its signature covers the text);
- ``system`` / ``tools`` changing between turns — every turn misses the prompt cache.

``--upstream ORIGIN`` puts a real provider behind the recorder: each request is
recorded, then relayed with the credential taken from the environment
(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN; CODEX_API_KEY for codex), and
``--model`` picks the model.  A real model writes the loop itself, so the checks
of the scripted loop give way to one that the provider accepted every turn.  An
agent without a fake upstream, codex for now, needs ``--upstream``.

The temp dir is kept for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import claude
import codex
from common import CANARIES, DUMMY_KEY, EMAIL, GIT_USER, IP, ORG, PERSON, TOKEN, Agent, Record

AGENTS: dict[str, Agent] = {"claude": claude.AGENT, "codex": codex.AGENT}
CONFIG = f"""\
detector:
  type: regex
  terms:
    PERSON: ["{PERSON}", "{GIT_USER}"]
    ORGANIZATION: ["{ORG}"]
"""

BARE = "PRIVYX_"  # what every token holds, with or without its < >
RECORDS: list[Record] = []
REAL = ""  # origin of the provider to relay to; empty answers from ``FAKE``
FAKE: Callable[[str, dict[str, Any]], tuple[bytes, str]] | None = None  # the agent's


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


class Upstream(BaseHTTPRequestHandler):
    """Records each forwarded request; replies from ``FAKE``, or from ``REAL`` if set."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.loads(raw or b"{}")
        record = self._record(body)
        if REAL:
            self._relay(raw, record)
        elif FAKE:
            self._send(*FAKE(self.path, body))

    def do_GET(self) -> None:  # a transparent proxy forwards any path
        record = self._record(None)
        if REAL:
            self._relay(None, record)
        else:
            self._send(b'{"type": "error"}', "application/json", 404)

    def _record(self, body: dict[str, Any] | None) -> Record:
        headers = dict(self.headers)
        if REAL:  # the temp dir is kept: no real credential in it
            headers = {
                k: "[redacted]" if k.lower() in ("authorization", "x-api-key") else v
                for k, v in headers.items()
            }
        record = {"path": self.path, "headers": headers, "body": body}
        RECORDS.append(record)
        return record

    def _relay(self, raw: bytes | None, record: Record) -> None:
        """Forward the request to ``REAL``, stream its reply back, and record it."""
        # No Accept-Encoding goes out, so the recorded reply is readable.
        drop = ("host", "content-length", "connection", "accept-encoding")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in drop}
        request = urllib.request.Request(REAL + self.path, raw, headers, method=self.command)
        try:
            reply = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as exc:
            reply = exc
        except OSError as exc:  # the provider is unreachable
            record["status"], record["reply"] = 502, str(exc)
            self._send(str(exc).encode(), "text/plain", 502)
            return
        self.send_response(reply.status)
        for k, v in reply.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(k, v)
        self.end_headers()  # no length: the body ends when this connection closes
        chunks = []
        while chunk := reply.read1(65536):
            chunks.append(chunk)
            self.wfile.write(chunk)
            self.wfile.flush()
        record["status"], record["reply"] = reply.status, b"".join(chunks).decode(errors="replace")

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
    """Create the canary-laden workspace and the Privyx config; return the workspace."""
    work = root / f"{ORG.lower()}-app"  # the path lands in the agent's environment section
    work.mkdir()
    (root / "home").mkdir()
    for name in ("CLAUDE.md", "AGENTS.md"):
        (work / name).write_text(f"# Project\n\nOwner: {PERSON} <{EMAIL}> at {ORG}.\n")
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


def main(name: str, transparent: bool, real: str, model: str | None) -> int:
    global REAL, FAKE
    agent = AGENTS[name]
    REAL, FAKE = real.rstrip("/"), agent.fake
    if not REAL and not FAKE:
        raise SystemExit(f"{name} has no fake upstream here; it needs --upstream")
    creds = {k: os.environ[k] for k in agent.creds if k in os.environ}
    if REAL and not creds:
        raise SystemExit(f"--upstream needs {' or '.join(agent.creds)} in the environment")

    root = Path(tempfile.mkdtemp(prefix="privyx-e2e-"))
    home = root / "home"
    strip = ("ANTHROPIC_", "CLAUDE", "OPENAI_", "CODEX_", "PRIVYX_", "XDG_CONFIG_HOME", "GIT_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(strip)}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_NOSYSTEM="1",
        PRIVYX_AUDIT_PATH=str(root / "audit.log"),
    )
    env.update(creds if REAL else {agent.creds[0]: DUMMY_KEY})
    work = seed(root, env)
    mcp = {
        "command": sys.executable,
        "args": [os.path.abspath(__file__), "--mcp", str(root / "mcp-calls.jsonl")],
    }
    env.update(agent.setup(root, mcp, transparent))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{server.server_address[1]}"

    argv = agent.args(root) + (["--model", model] if model else [])
    proxy = None
    if transparent:
        proxy, url = start_proxy(root, env, upstream)
        point_env, point_args = agent.point(url)
        env.update(point_env)
        cmd = [name, *point_args, *argv]
    else:
        cmd = ["privyx", "run", name, "-c", str(root / "privyx.yaml"),
               "--upstream", upstream + agent.endpoint, "--", *argv]  # fmt: skip
    timeout = 600 if REAL else 180
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
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out = (proc.communicate()[0] or "") + f"\n[e2e] timed out after {timeout}s\n"
    if proxy is not None:
        proxy.terminate()
        proxy.wait()
    server.shutdown()

    (root / "agent.out").write_text(out)
    with open(root / "upstream.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in RECORDS)

    failed = False
    found: dict[tuple[str, str], tuple[list[int], str]] = {}
    for n, record in enumerate(RECORDS, 1):
        sent = {k: record[k] for k in ("path", "headers", "body")}  # not the provider's reply
        for path, canary, context in leaks(sent):
            found.setdefault((path, canary), ([], context))[0].append(n)
    paths = ", ".join(dict.fromkeys(r["path"] for r in RECORDS))
    mode = "privyx proxy --transparent" if transparent else "privyx run"
    print(f"{mode}, {name}: upstream saw {len(RECORDS)} request(s) [{paths}]; "
          f"exit {proc.returncode}")  # fmt: skip
    for (path, canary), (reqs, context) in found.items():
        failed = True
        print(f"LEAK  {canary!r} at {path}  (req {','.join(map(str, reqs))})\n      …{context!r}…")

    def check(ok: bool, what: str, detail: str) -> None:
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what}: {detail}")

    # A model may write a token without its < >, or its id alone: either counts
    # as a missed restore.  A counter id is too short to tell from other text.
    issued = TOKEN.findall(json.dumps([r["body"] for r in RECORDS]))
    ids = {token[:-1].rpartition("_")[2] for token in issued}

    def leftover(text: str) -> bool:
        return BARE in text or any(part in text for part in ids if len(part) >= 8)

    # A real model may also skip the tool, decline, or rephrase; that says
    # nothing about Privyx.
    calls = root / "mcp-calls.jsonl"
    args = calls.read_text().strip() if calls.exists() else ""
    if REAL and not args:
        print("note  MCP tool: the model never called it")
    else:
        check(bool(args) and not leftover(args), "MCP tool args restored", args or "(never called)")
    # The last one: an agent that prints its transcript shows the prompt first.
    echo = next((ln for ln in reversed(out.splitlines()) if "Echo:" in ln), "")
    answered = all(v in echo for v in (PERSON, ORG, EMAIL, IP))
    if REAL and not answered and not leftover(out):
        print("note  final answer: no token left in it, but no Echo line with the four values")
    else:
        check(answered and not leftover(out), "final answer restored", echo or "(no Echo line)")

    main_records = [r for r in RECORDS if agent.main(r)]
    bodies = [r["body"] for r in main_records]
    if REAL:
        refused = [f"{r['status']} {r['reply'][:200]}" for r in main_records if r["status"] >= 400]
        accepted = bool(main_records) and not refused
        check(accepted, "provider accepted every turn", f"{len(main_records)} turn(s)")
        for reply in dict.fromkeys(refused):
            print(f"      {reply}")
    elif agent.checks:
        for result in agent.checks(main_records):
            check(*result)
    for key in agent.stable:
        moved = drift(bodies, key)
        check(not moved, f"{key} stable across turns", moved or f"{len(bodies)} turn(s)")

    print(f"artifacts: {root}  (upstream.jsonl, agent.out, audit.log, mcp-calls.jsonl)")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mcp"]:
        mcp_server(sys.argv[2])
    else:
        parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
        parser.add_argument("--agent", choices=AGENTS, default="claude")
        parser.add_argument("--transparent", action="store_true", help="through `privyx proxy`")
        parser.add_argument("--upstream", default="", help="origin of a real provider to relay to")
        parser.add_argument("--model", help="model the agent asks for")
        opts = parser.parse_args()
        sys.exit(main(opts.agent, opts.transparent, opts.upstream, opts.model))
