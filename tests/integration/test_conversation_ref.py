"""``conversation_ref`` on ``proxy.request``: a keyed hash of an agent's own conversation ID."""

from __future__ import annotations

import json
import logging
import re
import stat
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from privyx.agents import conversation  # noqa: E402
from privyx.cli.main import cli  # noqa: E402
from privyx.detectors import RegexDetector  # noqa: E402
from privyx.engine import PrivacyEngine  # noqa: E402
from privyx.events import RequestReceived  # noqa: E402
from privyx.features import AuditTrail, Metrics  # noqa: E402
from privyx.observability.audit import AuditLogger  # noqa: E402
from privyx.operators._pseudonym import PseudonymOperator  # noqa: E402
from privyx.policies import DefaultPolicy  # noqa: E402
from privyx.proxy import Pipeline  # noqa: E402
from privyx.proxy._asgi import create_app  # noqa: E402
from privyx.testing import listening  # noqa: E402
from privyx.vaults import MemoryVault  # noqa: E402

#: Made up, in the shapes Claude Code and Codex send.
CLAUDE = "3f1c9a52-7b4e-4d21-9c6a-0e5d8f2b1a77"
OTHER_CLAUDE = "8d2e4f10-5a6b-4c7d-8e9f-a0b1c2d3e4f5"
CODEX = "01a2167d-c229-7c50-8411-2b0f3bfc4b87"
CODEX_CHILD = "01a21680-1973-7cd1-87e9-957ef36daf11"

REF = re.compile(r"c1\.[0-9a-f]{8}\.[0-9a-f]{32}")


def _upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "resp_1", "object": "response", "output": []})
        reply = {"id": "msg_1", "type": "message", "role": "assistant", "content": []}
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _served(
    trail: AuditTrail, requests: list[tuple[str, dict[str, str], dict[str, Any]]]
) -> tuple[list[httpx.Response], str]:
    """Send ``requests`` through a proxy with ``trail`` on; the replies and ``/metrics``."""
    metrics = Metrics()
    events = await listening(trail, metrics)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    proxy = Pipeline(
        engine,
        origin="https://up.test",
        client=_upstream(),
        audit=AuditLogger(events=events),
        events=events,
    )
    app = create_app(proxy, metrics=metrics)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as client:
        replies = [await client.post(path, headers=h, json=body) for path, h, body in requests]
        text = (await client.get("/metrics")).text
    await trail.close()
    return replies, text


def _claude(session: str | None, text: str = "hi") -> tuple[str, dict[str, str], dict[str, Any]]:
    headers = {"x-claude-code-session-id": session} if session else {}
    return "/v1/messages", headers, {"model": "m", "messages": [{"role": "user", "content": text}]}


def _codex(session: str, thread: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    headers = {"session-id": session, "thread-id": thread}
    return "/v1/responses", headers, {"model": "m", "input": "hi"}


def _requests(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in records if r["event"] == "proxy.request"]


async def test_a_conversation_has_one_reference_and_its_id_is_kept_nowhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "audit.log"
    trail = AuditTrail(path=str(path), conversation_ref=True)
    replies, metrics = await _served(
        trail,
        [
            _claude(CLAUDE, "hi"),
            _claude(CLAUDE, "and again"),  # the next turn
            _claude(OTHER_CLAUDE, "hi"),  # a new launch with the same opener
            _claude(None),  # no agent says which conversation
            _codex(CODEX, CODEX),
            _codex(CODEX, CODEX_CHILD),  # its subagent: a thread of its own
        ],
    )

    refs = [r.get("conversation_ref") for r in _requests(path)]
    assert all(REF.fullmatch(ref) for ref in refs[:3] + refs[4:])
    assert refs[0] == refs[1] != refs[2]
    assert refs[3] is None
    assert refs[4] == refs[5] not in refs[:3]
    for native in (CLAUDE, OTHER_CLAUDE, CODEX, CODEX_CHILD):
        assert native not in path.read_text()
        assert native not in caplog.text
        assert native not in metrics
        assert all(native not in str(reply.headers) for reply in replies)


async def test_a_reference_comes_from_claude_codes_metadata_without_its_header(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.log"
    user = json.dumps({"device_id": "d" * 64, "account_uuid": "", "session_id": CLAUDE})
    path_, _, body = _claude(None)
    await _served(
        AuditTrail(path=str(path), conversation_ref=True),
        [_claude(CLAUDE), (path_, {}, {**body, "metadata": {"user_id": user}})],
    )
    first, second = _requests(path)
    assert first["conversation_ref"] == second["conversation_ref"]


async def test_off_by_default_the_trail_hears_no_request(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    trail = AuditTrail(path=str(path))
    events = await listening(trail)
    assert not events.wants(RequestReceived)
    await trail.close()
    await _served(AuditTrail(path=str(path)), [_claude(CLAUDE)])
    assert "conversation_ref" not in path.read_text()
    assert not (tmp_path / "audit.log.key").exists()


async def test_the_auto_key_is_kept_beside_the_trail_for_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    for _ in range(2):  # a restart reads the same key
        await _served(AuditTrail(path=str(path), conversation_ref=True), [_claude(CLAUDE)])
    key = tmp_path / "audit.log.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    first, second = _requests(path)
    assert first["conversation_ref"] == second["conversation_ref"]
    other = tmp_path / "other.log"
    keyed = AuditTrail(path=str(other), key="b" * 64, conversation_ref=True)
    await _served(keyed, [_claude(CLAUDE)])
    assert _requests(other)[0]["conversation_ref"] != first["conversation_ref"]


@pytest.mark.parametrize(
    ("headers", "body", "found"),
    [
        ({"x-claude-code-session-id": CLAUDE}, {}, ("claude", CLAUDE)),
        ({}, {"metadata": {"user_id": json.dumps({"session_id": CLAUDE})}}, ("claude", CLAUDE)),
        ({}, {"metadata": {"user_id": "user_abc_account__session_x"}}, None),  # an older form
        ({}, {"metadata": {"user_id": "{not json"}}, None),
        ({}, {"metadata": {"user_id": "[1]"}}, None),
        pytest.param(
            {},
            {"metadata": {"user_id": "{" + '"a":[' * 100_000 + "]" * 100_000 + "}"}},
            None,
            id="nested-too-deep",
        ),
        ({}, {"metadata": "x"}, None),
        ({"thread-id": CODEX}, {}, ("codex", CODEX)),
        ({"session-id": CODEX, "thread-id": CODEX_CHILD}, {}, ("codex", CODEX)),
        ({"x-request-id": CLAUDE}, {}, None),
    ],
)
def test_the_conversation_id_comes_only_from_an_agents_own_signal(
    headers: dict[str, str], body: dict[str, Any], found: tuple[str, str] | None
) -> None:
    assert conversation(headers, body) == found


async def test_audit_find_lists_the_events_of_one_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.log"
    await _served(
        AuditTrail(path=str(path), conversation_ref=True),
        [_claude(CLAUDE), _claude(OTHER_CLAUDE), _claude(CLAUDE, "again")],
    )
    monkeypatch.setenv("PRIVYX_FEATURES__AUDIT__PATH", str(path))
    runner = CliRunner()

    found = runner.invoke(cli, ["audit", "find", "--conversation", "-"], input=CLAUDE + "\n")

    assert found.exit_code == 0, found.output
    wanted = {r["request_id"] for r in _requests(path) if r["path"] == "/v1/messages"}
    lines = found.output.splitlines()
    shown = {line.split()[2] for line in lines}
    assert len(shown) == 2 and shown < wanted  # the first and third request
    assert {line.split()[1] for line in lines} >= {"proxy.request", "proxy.response"}
    assert CLAUDE not in found.output

    unknown = runner.invoke(cli, ["audit", "find", "--conversation", "-"], input="nope\n")
    assert unknown.exit_code == 1
    assert "no events of that conversation" in unknown.output
    held = tmp_path / "conversation"
    held.write_text(CLAUDE)
    assert runner.invoke(cli, ["audit", "find", "--conversation", str(held)]).output == found.output
    # The readers of the trail take the new field in their stride.
    assert runner.invoke(cli, ["audit", "stats"]).exit_code == 0
    tail = runner.invoke(cli, ["audit", "tail", "--no-follow"])
    assert tail.exit_code == 0 and "conversation_ref=c1." in tail.output


def test_audit_find_without_a_key_says_none_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.log"
    path.write_text("")
    monkeypatch.setenv("PRIVYX_FEATURES__AUDIT__PATH", str(path))
    result = CliRunner().invoke(cli, ["audit", "find", "--conversation", "-"], input=CLAUDE)
    assert result.exit_code == 1
    assert "features.audit.conversation_ref" in result.output
