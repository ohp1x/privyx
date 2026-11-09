"""``audit.tap``: each masked exchange before and after masking, owner-only."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.core.engine import PrivacyEngine
from privyx.observability.tap import Tap, tap_file
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"


def _upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    text = f"Sent to {body['messages'][-1]['content'].split()[-1]}."
    if body.get("stream"):
        events = [{"choices": [{"delta": {"content": part}}]} for part in (text[:12], text[12:])]
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _proxy(tap: Tap | None) -> TransparentProxy:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    return TransparentProxy(engine, origin="https://up.test", client=client, tap=tap)


async def _exchange(proxy: TransparentProxy, *, stream: bool = False) -> str:
    body = {"stream": stream, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    result = await proxy.handle(
        method="POST",
        path="v1/chat/completions",
        headers={"x-privyx-session": "ses_tap"},
        body=json.dumps(body).encode(),
    )
    if result.stream is not None:
        return "".join([chunk async for chunk in result.stream])
    return result.body.decode()


@pytest.mark.parametrize("stream", [False, True])
async def test_tap_records_both_sides_of_an_exchange(tmp_path: Path, stream: bool) -> None:
    proxy = _proxy(Tap(tmp_path / "tap"))

    reply = await _exchange(proxy, stream=stream)

    assert EMAIL in reply
    path = tmp_path / "tap" / "ses_tap.jsonl"
    request, response = (json.loads(line) for line in path.read_text().splitlines())
    assert request["stage"] == "request" and request["path"] == "/v1/chat/completions"
    assert EMAIL in json.dumps(request["before"])
    assert EMAIL not in json.dumps(request["after"])
    assert response["stage"] == "response" and response["stream"] is stream
    assert response["request_id"] == request["request_id"]
    assert EMAIL not in json.dumps(response["upstream"])
    assert EMAIL in json.dumps(response["restored"])
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_no_tap_writes_nothing(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_exchange(_proxy(None)))
    assert list(tmp_path.iterdir()) == []


def test_a_client_session_id_never_becomes_a_path(tmp_path: Path) -> None:
    path = tap_file(tmp_path, "../../etc/passwd")
    assert path.parent == tmp_path
    assert path.name.startswith("_") and ".." not in path.name
    # No session can name the file another one is hashed to.
    hashed = tap_file(tmp_path, "session/one")
    assert tap_file(tmp_path, hashed.stem) != hashed


async def test_audit_watch_shows_what_masking_changed(tmp_path: Path) -> None:
    await _exchange(_proxy(Tap(tmp_path / "tap")), stream=True)
    config = tmp_path / "c.yaml"
    config.write_text(f"audit:\n  tap: true\n  tap_dir: {tmp_path / 'tap'}\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["audit", "watch", "-c", str(config), "--no-follow"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("request" in line and "/v1/chat/completions" in line for line in lines)
    assert f"  mail [-{EMAIL}-]{{+<PRIVYX_EMAIL_1>+}}" in lines
    assert any("response" in line and "status 200 stream" in line for line in lines)
    assert f"  Sent to [-<PRIVYX_EMAIL_1>-]{{+{EMAIL}+}}." in lines

    one = CliRunner().invoke(cli, ["audit", "watch", "-c", str(config), "--no-follow", "ses_x"])
    assert one.output == ""
