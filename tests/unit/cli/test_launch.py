"""A launch plan resolves env once and supplies both display and subprocess."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from click.testing import CliRunner

from privyx.cli.commands.run import _show_plan, _spawn
from privyx.cli.launch import build_launch_plan, profile_target
from privyx.cli.main import cli
from privyx.config.loader import load_config
from privyx.config.schema import AgentConfig, Settings
from privyx.core.errors import ConfigError


def test_explicit_adapter_supports_wrapper_and_env_can_disable_it() -> None:
    profile = AgentConfig(command="company-wrapper", adapter="claude")
    target = profile_target("work", profile)
    plan = build_launch_plan(target, ["--continue"], Settings(), 1234, ())
    assert plan.argv[:1] == ["--settings"]
    assert "http://127.0.0.1:1234" in plan.argv[1]
    env_only = profile_target("claude", AgentConfig(command="claude", adapter="env"))
    assert build_launch_plan(env_only, ["--continue"], Settings(), 1234, ()).argv == ["--continue"]


async def test_preview_and_spawn_share_argv_env_snapshot_and_port_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PARENT_VALUE", "shell")
    monkeypatch.setenv("FLAG", "shell-flag")
    monkeypatch.setenv("SOURCE_TOKEN", "private-reference-value")
    profile = AgentConfig(
        command="wrapper",
        adapter="codex",
        args=["exec", "-c", "model=work"],
        env={"FLAG": "profile-flag", "BASE_URL": "https://bypass.example"},
        env_from={"AUTH_TOKEN": "SOURCE_TOKEN"},
        env_vars=["BASE_URL"],
        base_path="/v1",
    )
    plan = build_launch_plan(
        profile_target("work", profile),
        ["--", "prompt with spaces"],
        Settings(),
        0,
        ("BASE_URL",),
        name="work",
    )
    assert "<auto>" in plan.base_url
    bound = plan.bind_port(12345)
    assert bound.environment["BASE_URL"] == "http://127.0.0.1:12345/v1"
    assert bound.environment["FLAG"] == "profile-flag"
    assert bound.environment["PARENT_VALUE"] == "shell"
    assert bound.environment["AUTH_TOKEN"] == "private-reference-value"
    assert bound.argv[-2:] == ["--", "prompt with spaces"]
    assert bound.argv.count("-c") == 3  # adapter, profile, adapter repeated after config
    monkeypatch.setenv("SOURCE_TOKEN", "changed-after-plan")
    monkeypatch.setenv("PARENT_VALUE", "changed-after-plan")
    _show_plan(bound)
    shown = capsys.readouterr().out
    assert "FLAG=***" in shown and "AUTH_TOKEN=***" in shown
    assert "private-reference-value" not in shown
    assert "profile-flag" not in shown
    assert "private-reference-value" not in repr(bound)
    process = AsyncMock()
    process.wait.return_value = 7
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    assert await _spawn(bound) == 7
    spawn.assert_awaited_once_with(bound.command, *bound.argv, env=bound.environment)
    assert spawn.call_args.kwargs["env"]["PARENT_VALUE"] == "shell"
    assert spawn.call_args.kwargs["env"]["AUTH_TOKEN"] == "private-reference-value"


@pytest.mark.parametrize("dry_run", [True, False])
def test_missing_env_reference_fails_before_any_resources_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text(
        "agents:\n  work:\n    extends: claude\n    env_from: {AUTH_TOKEN: ABSENT_WORK_TOKEN}\n"
    )
    monkeypatch.delenv("ABSENT_WORK_TOKEN", raising=False)
    forbidden = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr("privyx.cli.commands.run._run_target", forbidden)
    flags = ["--dry-run"] if dry_run else []
    result = CliRunner().invoke(cli, ["run", "-c", str(config), *flags, "work"])
    assert result.exit_code == 1, result.output
    assert "requires environment variable ABSENT_WORK_TOKEN" in result.output
    forbidden.assert_not_called()


def test_config_dump_and_preview_hide_custom_env_and_default_arg_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "work": {
                        "extends": "claude",
                        "env": {"CUSTOM_FLAG": "private-custom-value"},
                        "env_from": {"AUTH_TOKEN": "WORK_TOKEN"},
                        "args": ["--api-key", "private-arg-key"],
                    }
                }
            }
        )
    )
    monkeypatch.setenv("WORK_TOKEN", "private-ref-value")
    for args in (["config", "--show"], ["run", "--dry-run", "work"]):
        result = CliRunner().invoke(cli, [*args, "-c", str(config)])
        assert result.exit_code == 0, result.output
        for secret in ("private-custom-value", "private-ref-value", "private-arg-key"):
            assert secret not in result.output
    # Inspection lists definitions without resolving secrets for unused profiles.
    monkeypatch.delenv("WORK_TOKEN")
    assert CliRunner().invoke(cli, ["run", "--list", "-c", str(config)]).exit_code == 0


def test_inherited_sources_are_visible_in_preview(tmp_path: Path) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text("agents:\n  work:\n    extends: claude\n    env: {CUSTOM_FLAG: enabled}\n")
    sources: dict[str, str] = {}
    settings = load_config(config, agent="work", sources=sources)
    plan = build_launch_plan(
        profile_target("work", settings.agents["work"]),
        [],
        settings,
        1234,
        ("ANTHROPIC_BASE_URL",),
        name="work",
        sources=sources,
    )
    assert plan.sources["agents.work.command"] == "defaults"
    assert plan.env_sources["CUSTOM_FLAG"] == str(config)
    assert plan.env_sources["ANTHROPIC_BASE_URL"] == "defaults"


def test_invalid_cli_proxy_env_is_reported() -> None:
    result = CliRunner().invoke(cli, ["run", "--dry-run", "claude", "--env-var", "BAD=NAME"])
    assert result.exit_code == 1
    assert "valid environment variable names" in result.output


def test_reference_reads_shell_only_and_accepts_an_existing_empty_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = AgentConfig(
        command="tool", env={"SOURCE": "profile-value"}, env_from={"DEST": "SOURCE"}
    )
    target = profile_target("work", profile)
    monkeypatch.delenv("SOURCE", raising=False)
    with pytest.raises(ConfigError, match="requires environment variable SOURCE"):
        build_launch_plan(target, [], Settings(), 1234, ())
    monkeypatch.setenv("SOURCE", "")
    assert build_launch_plan(target, [], Settings(), 1234, ()).environment["DEST"] == ""


def test_cli_finds_command_using_profile_path(tmp_path: Path) -> None:
    tool = tmp_path / "profile-path-tool"
    tool.write_text("#!/bin/sh\nexit 9\n")
    tool.chmod(0o700)
    config = tmp_path / "profiles.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "work": {
                        "command": tool.name,
                        "env": {"PATH": str(tmp_path)},
                        "env_vars": ["WORK_BASE_URL"],
                    }
                },
                "audit": {"path": str(tmp_path / "audit.log")},
            }
        )
    )
    result = CliRunner().invoke(cli, ["run", "--no-anchor", "-c", str(config), "work"])
    assert result.exit_code == 9, result.output


def test_pi_adapter_loads_the_shipped_extension_before_user_args() -> None:
    target = profile_target("pi", AgentConfig(command="pi", adapter="pi"))
    plan = build_launch_plan(target, ["--model", "x"], Settings(), 1234, ("PRIVYX_PI_BASE_URL",))
    assert plan.argv[0] == "-e" and Path(plan.argv[1]).is_file()
    assert plan.argv[2:] == ["--model", "x"]
    assert plan.environment["PRIVYX_PI_BASE_URL"] == "http://127.0.0.1:1234"


# The extension's guards, run in node against a stand-in for pi's API.
_PI_GUARDS = """
const ext = (await import(process.argv[1])).default;
const handlers = {}, registered = [], selected = [];
const pi = {
  registerProvider: (name, config) => registered.push([name, config.baseUrl]),
  on: (event, handler) => { handlers[event] = handler; },
  setModel: async (model) => { selected.push(model.id); return true; },
};
ext(pi);
const proxy = { provider: "anthropic", id: "a", baseUrl: process.env.PRIVYX_PI_BASE_URL };
const other = { provider: "deepseek", id: "d", baseUrl: "https://api.deepseek.com" };
const notes = [];
const ctx = (model) => ({ model, ui: { notify: (m) => notes.push(m) } });
await handlers.model_select({ model: other, previousModel: proxy }, ctx(other));
const allowed = handlers.input({}, ctx(proxy)).action;
const blocked = handlers.input({}, ctx(other)).action;
const blockedExit = process.exitCode;
process.exitCode = 0;
const payload = { registered, selected, allowed, blocked, blockedExit, notes: notes.length };
console.log(JSON.stringify(payload));
"""


def test_pi_extension_keeps_every_model_on_the_proxy(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    extension = profile_target("pi", AgentConfig(command="pi", adapter="pi"))
    path = build_launch_plan(extension, [], Settings(), 1, ()).argv[1]
    result = subprocess.run(
        [node, "--input-type=module", "-e", _PI_GUARDS, Path(path).as_uri()],
        env={"PRIVYX_PI_PROVIDER": "anthropic", "PRIVYX_PI_BASE_URL": "http://127.0.0.1:1"},
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    assert json.loads(result.stdout) == {
        "registered": [["anthropic", "http://127.0.0.1:1"]],
        "selected": ["a"],  # switching away put the proxied model back
        "allowed": "continue",
        "blocked": "handled",  # a prompt never reaches a model outside the proxy
        "blockedExit": 1,
        "notes": 2,
    }
