"""Harvest the local identity only for an opted-in run."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from privyx.cli import workspace
from privyx.cli.commands.run import _harvest
from privyx.cli.main import cli
from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.schemas import transform_request
from privyx.vault.memory import MemoryVault


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "posix":
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="alice"))
    else:
        monkeypatch.setattr("getpass.getuser", lambda: "alice")


@pytest.mark.parametrize("harvest", [False, True])
async def test_harvest_masks_identity_in_safeguards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: None, harvest: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    if harvest:
        assert _harvest(settings) == "1 PERSON, 0 .env value(s)"
    configs = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    engine = PrivacyEngine(
        detector=build_detector([d.model_dump() for d in configs]),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    payload = {
        "messages": [],
        "safeguards": [{"classifier_context": {"user_identity": "alice"}}],
    }
    out = await transform_request(payload, engine, session.session_id)
    value = out["safeguards"][0]["classifier_context"]["user_identity"]
    assert value.startswith("<PRIVYX_PERSON_") if harvest else value == "alice"
    assert payload["safeguards"][0]["classifier_context"]["user_identity"] == "alice"
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


def test_scan_does_not_collect_os_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: None
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["scan", "--yes"])
    assert result.exit_code == 0, result.output
    assert "alice" not in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX account lookup")
def test_username_comes_from_os_not_environment(
    monkeypatch: pytest.MonkeyPatch, identity: None
) -> None:
    for key in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.setenv(key, "bob")
    assert workspace.os_user_terms() == [workspace.Found("PERSON", "alice", "OS user")]


@pytest.mark.skipif(os.name != "posix", reason="POSIX account lookup")
@pytest.mark.parametrize("name", ["root", "ubuntu", "admin", "user", "ROOT", "ab", ""])
def test_generic_and_short_usernames_are_skipped(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name=name))
    assert workspace.os_user_terms() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX account lookup")
@pytest.mark.parametrize("error", [KeyError, OSError])
def test_missing_os_identity_does_not_stop_harvest(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    import pwd

    def unavailable(uid: int) -> None:
        raise error("account unavailable")

    monkeypatch.setattr(pwd, "getpwuid", unavailable)
    assert workspace.os_user_terms() == []


def test_identity_is_not_shown_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: None
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["run", "--harvest", "--dry-run", "claude"])
    assert result.exit_code == 0, result.output
    assert "Harvest: 1 PERSON, 0 .env value(s)" in result.output
    assert "alice" not in result.output


def test_identity_is_deduplicated_with_git_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        workspace, "git_terms", lambda *a: [workspace.Found("PERSON", "Alice", "git")]
    )
    settings = Settings()
    assert _harvest(settings) == "1 PERSON, 0 .env value(s)"
    assert json.dumps(settings.model_dump()).count('"Alice"') == 1
