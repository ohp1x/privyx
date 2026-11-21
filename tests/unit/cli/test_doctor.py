"""Diagnostics honor configuration without exposing credentials or leaving sessions."""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.core import VaultError

doctor_module = importlib.import_module("privyx.cli.commands.doctor")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in os.environ:
        if key.startswith("PRIVYX_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PRIVYX_NO_DISCOVERY", "1")
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("field", ["upstream_url", "provider.base_url"])
def test_doctor_redacts_url_credentials(tmp_path: Path, field: str) -> None:
    config = tmp_path / "config.yaml"
    url = "https://alice:example-password@example.com/v1?key=example-query#example-fragment"
    config.write_text(
        f"upstream_url: {url}\n" if field == "upstream_url" else f"provider:\n  base_url: {url}\n"
    )

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert "example.com/v1" in result.output
    for credential in ("alice", "example-password", "example-query", "example-fragment"):
        assert credential not in result.output


def _sessions(db: Path) -> list[tuple[str, str, float]]:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()


@pytest.mark.parametrize("failure", [None, "vault", "proxy", "streaming"])
def test_doctor_cleans_only_its_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    from privyx.proxy.stream_router import StreamRouter
    from privyx.vaults._sqlite import SQLiteVault

    config = tmp_path / "config.yaml"
    db = tmp_path / "vault.db"
    config.write_text(f"vault:\n  type: sqlite\n  dsn: {db}\n")
    runner = CliRunner()
    seeded = runner.invoke(
        cli, ["mask", "-c", str(config), "--session", "keep", "alice@example.com"]
    )
    assert seeded.exit_code == 0, seeded.output
    before = _sessions(db)
    assert len(before) == 1

    if failure == "vault":
        save = SQLiteVault.save

        async def fail_save(self, session, **kwargs):
            if "doctor-probe-key" in session.mapping:
                raise VaultError("probe save failed")
            return await save(self, session, **kwargs)

        monkeypatch.setattr(SQLiteVault, "save", fail_save)
    elif failure == "proxy":

        async def fail_request(*args, **kwargs):
            raise RuntimeError("probe request failed")

        monkeypatch.setattr("privyx.proxy.schemas.transform_request", fail_request)
    elif failure == "streaming":

        def fail_stream(*args, **kwargs):
            raise RuntimeError("probe stream failed")

        monkeypatch.setattr(StreamRouter, "feed", fail_stream)

    for _ in range(2):
        result = runner.invoke(cli, ["doctor", "-c", str(config)])
        assert result.exit_code == (1 if failure else 0), result.output
        if failure:
            assert f"✗ {failure}:" in result.output
        assert _sessions(db) == before


@pytest.mark.parametrize(
    "contents, exercised",
    [
        ("detector:\n  type: yaml\n  terms:\n    ORGANIZATION: [apollo]\n", True),
        (
            "detector:\n  type: yaml\n  terms:\n    ORGANIZATION: [apollo]\n"
            "policy:\n  type: strict\n  allowed: [ORGANIZATION]\n",
            True,
        ),
        ("detector:\n  disable: [EMAIL]\n", False),
        ("detector:\n  type: yaml\n  patterns:\n    TICKET: 'TCK-[0-9]{4}'\n", False),
        ("policy:\n  type: strict\n  allowed: [ORGANIZATION]\n", False),
    ],
)
def test_doctor_respects_detector_and_policy_scope(
    tmp_path: Path, contents: str, exercised: bool
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(contents)

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert "apollo" not in result.output
    if exercised:
        assert "request transformed, 1 mapping(s) vaulted" in result.output
        assert "pseudonym restored across a split chunk" in result.output
    else:
        assert "proxy: skipped:" in result.output
        assert "streaming: skipped:" in result.output


@pytest.mark.parametrize("prefix", ["", "still visible: "])
def test_doctor_still_detects_a_broken_transform(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    async def unchanged(payload, engine, session_id, **kwargs):
        payload["messages"][0]["content"] = prefix + payload["messages"][0]["content"]
        return payload

    monkeypatch.setattr("privyx.proxy.schemas.transform_request", unchanged)
    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "✗ proxy:" in result.output


def test_doctor_still_detects_a_broken_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from privyx.proxy.stream_router import StreamRouter

    monkeypatch.setattr(StreamRouter, "feed", lambda self, chunk: [chunk])
    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "✗ streaming:" in result.output


def test_doctor_still_detects_a_broken_builtin_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    from privyx.core import Detection
    from privyx.detectors import RegexDetector

    monkeypatch.setattr(RegexDetector, "detect_sync", lambda *args: Detection())

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "✗ detector: configured detector missed the built-in email sample" in result.output


@pytest.mark.parametrize("term", ['apollo "test"', "apóllo", "apollo\nproject", "PERSON"])
def test_doctor_restores_configured_terms_without_printing_them(tmp_path: Path, term: str) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"detector": {"type": "yaml", "terms": {"PERSON": [term]}}}))

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert "streaming: pseudonym restored across a split chunk" in result.output
    if term != "PERSON":
        assert term not in result.output


@pytest.mark.parametrize("operator", ["redact", "hash", "partial", "encrypt"])
def test_doctor_handles_configured_operators(tmp_path: Path, operator: str) -> None:
    config = tmp_path / "config.yaml"
    key = f"  key: '{'ab' * 32}'\n" if operator == "encrypt" else ""
    config.write_text(f"operator:\n  type: {operator}\n{key}")

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    if operator == "redact":
        assert "streaming: skipped: operator" in result.output
    else:
        assert "streaming: pseudonym restored across a split chunk" in result.output


def test_doctor_closes_vaults_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from privyx.vaults._sqlite import SQLiteVault

    config = tmp_path / "config.yaml"
    config.write_text(f"vault:\n  type: sqlite\n  dsn: {tmp_path / 'vault.db'}\n")
    close = SQLiteVault.close
    closed = []

    async def fail_delete(self, session_id):
        raise VaultError("probe cleanup failed")

    async def record_close(self):
        await close(self)
        closed.append(self)

    monkeypatch.setattr(SQLiteVault, "delete", fail_delete)
    monkeypatch.setattr(SQLiteVault, "close", record_close)

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 1
    assert result.output.count("probe cleanup failed") == 3
    assert len(closed) == 3


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("allowed", ["PERSON", "EMAIL"])
def test_doctor_never_sends_configured_terms_to_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mixed: bool, allowed: str
) -> None:
    import json

    import yaml

    term = "apollo-private"
    prompts = []

    class Client:
        async def complete(self, prompt):
            prompts.append(prompt)
            text = prompt.split("TEXT:\n", 1)[1]
            email = "alice@example.com"
            if email in text:
                start = text.index(email)
                return json.dumps([[start, start + len(email), "EMAIL", email]])
            return "[]"

    monkeypatch.setattr("privyx.detectors._llm._load_client", lambda *args: Client())
    local = {"type": "yaml", "terms": {"PERSON": [term]}}
    # Unmixed, the terms come from a file below the one that picks `llm`.
    (tmp_path / "base.yaml").write_text(yaml.safe_dump({"detector": local}))
    detector = [local, {"type": "llm"}] if mixed else {"type": "llm"}
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "include": "base.yaml",
                "detector": detector,
                "policy": {"type": "strict", "allowed": [allowed]},
            }
        )
    )

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert prompts
    assert all(term not in prompt for prompt in prompts)
    if allowed == "EMAIL":
        assert "proxy: request transformed, 1 mapping(s) vaulted" in result.output
        assert "streaming: pseudonym restored across a split chunk" in result.output
    else:
        assert "proxy: skipped:" in result.output
        assert "streaming: skipped:" in result.output


REMOTE_DETECTOR = """
from privyx.core import Detection
from privyx.detectors import Detector

SCANNED = []


class RemoteDetector(Detector):
    name = "remote-example"

    async def detect(self, text, context):
        SCANNED.append(text)
        return Detection()


def setup(app):
    app.add(RemoteDetector)
"""


def test_doctor_never_sends_configured_terms_to_plugin_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import sys

    import yaml

    term = "apollo-private"
    (tmp_path / "remote_example_plugin.py").write_text(REMOTE_DETECTOR)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "remote_example_plugin", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "plugins": {"modules": ["remote_example_plugin"]},
                "detector": [
                    {"type": "yaml", "terms": {"PERSON": [term]}},
                    {"type": "remote-example"},
                ],
            }
        )
    )

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 0, result.output
    scanned = importlib.import_module("remote_example_plugin").SCANNED
    assert scanned
    assert all(term not in text for text in scanned)
    assert "proxy: skipped:" in result.output


@pytest.mark.parametrize("token, expected", [(None, 0), ("[TED-HIDDEN]", 0), ("", 0), ("TED", 1)])
def test_doctor_accounts_for_redaction_token(
    tmp_path: Path, token: str | None, expected: int
) -> None:
    import yaml

    operator = {"type": "redact"}
    if token is not None:
        operator["token"] = token
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"detector": {"type": "yaml", "terms": {"PERSON": ["TED"]}}, "operator": operator}
        )
    )

    result = CliRunner().invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == expected, result.output
    assert ("✗ proxy:" in result.output) == bool(expected)


def test_doctor_does_not_create_the_anchor_key(tmp_path: Path) -> None:
    from privyx.security.keys import default_anchor_secret_path

    result = CliRunner().invoke(cli, ["doctor"])

    assert "anchor=hmac" in result.output
    assert not default_anchor_secret_path().exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [(None, "✓ anchor key: "), ("", "✗ anchor key: "), ("k" * 64, "✓ anchor key: ")],
)
def test_doctor_checks_the_anchor_key_without_creating_it(
    content: str | None, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "keys" / "anchor.key"
    monkeypatch.setenv("PRIVYX_ANCHOR_KEY_PATH", str(key))
    if content is not None:
        key.parent.mkdir()
        key.write_text(content)

    result = CliRunner().invoke(cli, ["doctor"])

    assert expected in result.output
    assert key.exists() == (content is not None)
