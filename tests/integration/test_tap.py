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


def test_export_and_watch_keep_only_the_session_asked_for(tmp_path: Path) -> None:
    """Two sessions in one file, as on a case-insensitive file system."""
    tap = Tap(tmp_path / "tap")
    tap.record("ses_a", "req_1", "request", path="/v1/x", before={"m": "mine"}, after={"m": "A"})
    shared = tap_file(tap.directory, "ses_a")
    with shared.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "ses_A", "stage": "request", "after": "other"}) + "\n")
    config = tmp_path / "c.yaml"
    config.write_text(
        f"audit:\n  tap: true\n  path: {tmp_path / 'none.log'}\n  tap_dir: {tap.directory}\n",
        encoding="utf-8",
    )
    out = tmp_path / "a.zip"
    runner = CliRunner()

    runner.invoke(
        cli, ["session", "export", "ses_a", "--forensic", "-c", str(config), "-o", str(out)]
    )
    watched = runner.invoke(cli, ["audit", "watch", "ses_a", "-c", str(config), "--no-follow"])

    import zipfile

    with zipfile.ZipFile(out) as archive:
        transcript = archive.read("transcript.jsonl").decode()
    assert "ses_a" in transcript and "ses_A" not in transcript
    assert "ses_a" in watched.output and "ses_A" not in watched.output


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


def test_forensic_export_keeps_only_the_masked_side(tmp_path: Path) -> None:
    import asyncio
    import io
    import zipfile

    from privyx.observability.audit import AuditLogger

    log = io.StringIO()
    proxy = _proxy(Tap(tmp_path / "tap"))
    proxy._audit = AuditLogger(log)  # noqa: SLF001

    async def exchanges() -> None:
        await _exchange(proxy)
        await _exchange(proxy, stream=True)

    asyncio.run(exchanges())
    (tmp_path / "audit.log").write_text(log.getvalue(), encoding="utf-8")
    config = tmp_path / "c.yaml"
    config.write_text(
        f"audit:\n  path: {tmp_path / 'audit.log'}\n  tap_dir: {tmp_path / 'tap'}\n",
        encoding="utf-8",
    )
    out = tmp_path / "case.zip"
    runner = CliRunner()

    result = runner.invoke(
        cli, ["session", "export", "ses_tap", "--forensic", "-c", str(config), "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == ["events.jsonl", "summary.json", "transcript.jsonl"]
        everything = b"".join(archive.read(name) for name in archive.namelist())
        summary = json.loads(archive.read("summary.json"))
        transcript = archive.read("transcript.jsonl").decode().splitlines()
    assert EMAIL.encode() not in everything
    assert b"<PRIVYX_EMAIL_1>" in everything
    assert summary["events"]["proxy.response"] == 2
    assert summary["transcript_records"] == {"request": 2, "response": 2}
    assert {"before", "restored"}.isdisjoint(json.loads(transcript[0]))
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    again = runner.invoke(
        cli, ["session", "export", "ses_tap", "--forensic", "-c", str(config), "-o", str(out)]
    )
    assert again.exit_code == 1 and "cannot write" in again.output
    missing = runner.invoke(cli, ["session", "export", "ses_none", "--forensic", "-c", str(config)])
    assert missing.exit_code == 1 and "Nothing recorded" in missing.output
    no_flag = runner.invoke(cli, ["session", "export", "ses_tap", "-c", str(config)])
    assert no_flag.exit_code == 2
