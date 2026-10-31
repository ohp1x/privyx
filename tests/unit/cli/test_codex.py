"""Custom Codex providers keep their authentication while routing through Privyx."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.commands.run import TARGETS
from privyx.cli.launch import build_launch_plan, profile_target
from privyx.cli.main import cli
from privyx.config.schema import AgentConfig, Settings


def test_custom_provider_urls_are_snapshotted_without_auth_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    config = root / "config.toml"
    original = (
        'model_provider="router-one"\n'
        '[model_providers."router-one"]\n'
        'name="Company router"\nbase_url="http://127.0.0.1:9/v1"\n'
        'http_headers={Authorization="private-router-credential"}\n'
        '[model_providers.fallback]\nname="Fallback"\nenv_key="ROUTER_API_KEY"\n'
    )
    config.write_text(original)
    monkeypatch.setenv("CODEX_HOME", str(root))
    plan = build_launch_plan(TARGETS["codex"], [], Settings(), 0, ("OPENAI_BASE_URL",))
    assert config.read_text() == original
    config.write_text('model_provider="changed-after-plan"\n')
    argv = plan.bind_port(1234).argv
    overrides = [tomllib.loads(argv[i + 1]) for i, arg in enumerate(argv) if arg == "-c"]
    assert overrides == [
        {"openai_base_url": "http://127.0.0.1:1234/v1"},
        {"model_providers": {"fallback": {"base_url": "http://127.0.0.1:1234/v1"}}},
        {"model_providers": {"router-one": {"base_url": "http://127.0.0.1:1234/v1"}}},
    ]
    assert "private-router-credential" not in json.dumps(argv)
    assert "ROUTER_API_KEY" not in json.dumps(argv)


@pytest.mark.parametrize("flag", ["-c", "--config", "-cattached", "--config=attached"])
def test_cli_provider_definition_is_overridden_after_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    definition = (
        'model_providers.work={name="Work",base_url="http://127.0.0.1:9/v1",'
        "requires_openai_auth=false}"
    )
    option = (
        [flag, definition] if flag in {"-c", "--config"} else [flag.replace("attached", definition)]
    )
    argv = ["exec", *option, "--", "model_providers.prompt={base_url='https://ignored'}"]
    plan = build_launch_plan(TARGETS["codex"], argv, Settings(), 1234, ())
    override = 'model_providers.work.base_url="http://127.0.0.1:1234/v1"'
    assert plan.argv.count(override) == 2
    assert plan.argv[-4:] == ["-c", override, *argv[-2:]]
    assert not any(arg.startswith("model_providers.prompt.") for arg in plan.argv)


@pytest.mark.parametrize("profile_flag", ["-p", "--profile", "--profile=work"])
def test_profile_file_and_legacy_profiles_are_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_flag: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.old]\nmodel_provider="legacy"\n')
    (tmp_path / "work.config.toml").write_text('model_provider="profile-router"\n')
    argv = [profile_flag, "work"] if "=" not in profile_flag else [profile_flag]
    plan = build_launch_plan(TARGETS["codex"], argv, Settings(), 1234, ())
    assert 'model_providers.legacy.base_url="http://127.0.0.1:1234/v1"' in plan.argv
    assert 'model_providers.profile-router.base_url="http://127.0.0.1:1234/v1"' in plan.argv


def test_profile_environment_and_cd_select_config_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profile-home"
    root.mkdir()
    (root / "config.toml").write_text('model_provider="from-profile-env"\n')
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex/config.toml").write_text('[model_providers.project]\nname="Project"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "wrong-home"))
    target = profile_target(
        "work", AgentConfig(command="codex", adapter="codex", env={"CODEX_HOME": str(root)})
    )
    plan = build_launch_plan(target, ["--cd", str(project)], Settings(), 1234, ())
    assert 'model_providers.from-profile-env.base_url="http://127.0.0.1:1234"' in plan.argv
    assert 'model_providers.project.base_url="http://127.0.0.1:1234"' in plan.argv


def test_malformed_codex_config_fails_before_launch_without_printing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_key="private-unclosed-credential\n')
    result = CliRunner().invoke(cli, ["run", "--dry-run", "codex"])
    assert result.exit_code == 1
    assert "cannot read Codex config" in result.output
    assert "private-unclosed-credential" not in result.output


def test_provider_id_that_cannot_be_addressed_does_not_silently_bypass_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model_provider="router.with.dots"\n')
    result = CliRunner().invoke(cli, ["run", "--dry-run", "codex"])
    assert result.exit_code == 1
    assert "cannot safely override URL of Codex provider" in result.output
