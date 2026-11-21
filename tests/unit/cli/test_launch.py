"""A launch plan resolves env once and supplies both display and subprocess."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.app._run import plan_launch as build_launch_plan
from privyx.cli.commands.run import (
    _set_terminal_title,
    _show_plan,
    _show_running,
    _spawn,
)
from privyx.cli.main import cli
from privyx.config.schema import AgentConfig, Settings
from privyx.core import ConfigError

from ._launch import TARGETS, build_target

PI_UPSTREAMS = "the provider of each pi model"


@pytest.mark.parametrize("agent", [*TARGETS, "work"])
async def test_announcement_sets_agent_title_before_launch_and_allows_child_updates(
    monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    # A custom profile may run a Python wrapper; its title should name the profile.
    target = TARGETS.get(agent) or build_target(AgentConfig(command="python3"))
    plan = build_launch_plan(target, [], Settings(), 1234, target.env_vars, name=agent)
    terminal = io.StringIO()
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr("sys.stderr", terminal)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("click.echo", lambda *args, **kwargs: None)

    async def wait() -> int:
        assert terminal.getvalue() == f"\033]0;{agent}\007"
        terminal.write("\033]0;Agent's own session title\007")
        return 7

    process = AsyncMock()
    process.wait.side_effect = wait
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

    _show_running(plan)
    assert await _spawn(plan) == 7
    spawn.assert_awaited_once_with(plan.command, *plan.argv, env=plan.environment)
    assert terminal.getvalue() == f"\033]0;{agent}\007\033]0;Agent's own session title\007"


@pytest.mark.parametrize(("tty", "term"), [(False, "xterm-256color"), (True, "dumb")])
async def test_announcement_leaves_redirected_and_dumb_output_free_of_title_sequences(
    monkeypatch: pytest.MonkeyPatch, tty: bool, term: str
) -> None:
    plan = build_launch_plan(TARGETS["codex"], [], Settings(), 1234, ())
    terminal = io.StringIO()
    monkeypatch.setattr(terminal, "isatty", lambda: tty)
    monkeypatch.setattr("sys.stderr", terminal)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr("click.echo", lambda *args, **kwargs: None)
    process = AsyncMock()
    process.wait.return_value = 0
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=process))

    _show_running(plan)
    assert await _spawn(plan) == 0
    assert terminal.getvalue() == ""


def test_terminal_title_strips_path_and_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = io.StringIO()
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr("sys.stderr", terminal)
    monkeypatch.setenv("TERM", "xterm-256color")

    _set_terminal_title("/tools/codex\007\033\n\r\x7f\x9c")

    assert terminal.getvalue() == "\033]0;codex\007"


def test_explicit_type_supports_wrapper_and_env_can_disable_it() -> None:
    profile = AgentConfig(command="company-wrapper", type="claude")
    target = build_target(profile)
    plan = build_launch_plan(target, ["--continue"], Settings(), 1234, ())
    assert plan.argv[:1] == ["--settings"]
    assert "http://127.0.0.1:1234" in plan.argv[1]
    env_only = build_target(AgentConfig(command="claude", type="env"))
    assert build_launch_plan(env_only, ["--continue"], Settings(), 1234, ()).argv == ["--continue"]


async def test_preview_and_spawn_share_argv_env_snapshot_and_port_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PARENT_VALUE", "shell")
    monkeypatch.setenv("FLAG", "shell-flag")
    monkeypatch.setenv("SOURCE_TOKEN", "private-reference-value")
    profile = AgentConfig(
        command="wrapper",
        type="codex",
        args=["exec", "-c", "model=work"],
        env={"FLAG": "profile-flag", "BASE_URL": "https://bypass.example"},
        env_from={"AUTH_TOKEN": "SOURCE_TOKEN"},
        env_vars=["BASE_URL"],
        base_path="/v1",
    )
    plan = build_launch_plan(
        build_target(profile),
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
        build_target(settings.agents["work"]),
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
    target = build_target(profile)
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


def test_pi_type_loads_the_shipped_extension_before_user_args() -> None:
    target = build_target(AgentConfig(command="pi", type="pi"))
    plan = build_launch_plan(target, ["--model", "x"], Settings(), 1234, ("PRIVYX_PI_BASE_URL",))
    assert plan.argv[0] == "-e" and Path(plan.argv[1]).is_file()
    assert plan.argv[2:] == ["--model", "x"]
    assert plan.environment["PRIVYX_PI_BASE_URL"] == "http://127.0.0.1:1234"


def test_pi_type_passes_a_subcommand_first() -> None:
    """pi reads a subcommand only as its first argument; behind -e it is a prompt."""
    target = build_target(AgentConfig(command="pi", type="pi"))
    plan = build_launch_plan(target, ["list"], Settings(), 1234, ("PRIVYX_PI_BASE_URL",))
    assert plan.argv == ["list"]


def test_pi_follows_each_provider_unless_one_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_PI_PROVIDER", "openai")  # from an outer run, say
    target = build_target(Settings().agents["pi"])
    plan = build_launch_plan(target, ["--model", "x"], Settings(), 1234, ("PRIVYX_PI_BASE_URL",))
    assert plan.run_token and plan.environment["PRIVYX_PI_TOKEN"] == plan.run_token
    assert "PRIVYX_PI_PROVIDER" not in plan.environment
    assert plan.route == plan.upstream == PI_UPSTREAMS
    again = build_launch_plan(target, [], Settings(), 1234, ("PRIVYX_PI_BASE_URL",))
    assert again.run_token != plan.run_token


@pytest.mark.parametrize(
    "settings",
    [
        Settings(upstream_url="https://api.example.com"),
        Settings(proxy={"mode": "gateway"}),
        # Every pi provider would receive them: one provider, as before.
        Settings(provider={"api_key": "sk-" + "ant-0123456789abcdef"}),
        Settings(provider={"headers": {"x-auth": "secret"}}),
    ],
)
def test_pi_serves_one_provider_with_an_upstream_or_own_credentials(settings: Settings) -> None:
    target = build_target(AgentConfig(command="pi", type="pi", provider="openai"))
    plan = build_launch_plan(
        target,
        ["--model", "x"],
        settings,
        1234,
        ("PRIVYX_PI_BASE_URL",),
        sources={"provider.type": "CLI"},
    )
    assert plan.environment["PRIVYX_PI_PROVIDER"] == "openai"
    assert plan.env_sources["PRIVYX_PI_PROVIDER"] == "CLI"
    assert plan.run_token is None and "PRIVYX_PI_TOKEN" not in plan.environment


def test_pi_provider_from_env_from_serves_that_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PI_PROVIDER", "deepseek")
    profile = AgentConfig(
        command="pi", type="pi", env_from={"PRIVYX_PI_PROVIDER": "MY_PI_PROVIDER"}
    )
    plan = build_launch_plan(
        build_target(profile), [], Settings(), 1234, ("PRIVYX_PI_BASE_URL",)
    )
    assert plan.environment["PRIVYX_PI_PROVIDER"] == "deepseek"
    assert plan.run_token is None and "PRIVYX_PI_TOKEN" not in plan.environment


def test_pi_provider_preserves_explicit_environment_override() -> None:
    target = build_target(AgentConfig(
            command="pi",
            type="pi",
            provider="openai",
            env={"PRIVYX_PI_PROVIDER": "deepseek"},
        ),
    )
    plan = build_launch_plan(
        target,
        ["--model", "x"],
        Settings(),
        1234,
        ("PRIVYX_PI_BASE_URL",),
    )
    assert plan.environment["PRIVYX_PI_PROVIDER"] == "deepseek"
    assert plan.run_token is None


def test_cli_run_pi_dry_run_names_the_routing(home: Path) -> None:
    result = CliRunner().invoke(cli, ["run", "--dry-run", "pi"])
    assert result.exit_code == 0, result.output
    assert f"Upstream: {PI_UPSTREAMS} [pi]" in result.output
    assert "PRIVYX_PI_TOKEN=*** [new each run]" in result.output
    result = CliRunner().invoke(
        cli, ["run", "--dry-run", "-p", "openai", "-u", "https://api.example.com", "pi"]
    )
    assert result.exit_code == 0, result.output
    assert "Provider: openai [CLI]" in result.output
    assert "PRIVYX_PI_PROVIDER=*** [CLI]" in result.output


# The extension's guards, run in node against a stand-in for pi's API: its
# registry, where registering a provider's baseUrl moves all its models, and a
# login of provider "cop" that names a base URL of its own.
_PI_STAND_IN = """
const ext = (await import(process.argv[1])).default;
const models = JSON.parse(process.argv[2]);
const handlers = {}, registered = [], selected = [], notes = [];
const modelRegistry = {
  getAll: () => models,
  find: (provider, id) => models.find((m) => m.provider === provider && m.id === id),
  getApiKeyAndHeaders: async (m) =>
    m.provider === "cop" ? { ok: true, baseUrl: "https://cop.example.com/own" } : { ok: true },
};
const pi = {
  registerProvider: (name, config) => {
    registered.push([name, config]);
    for (const m of models) if (m.provider === name) m.baseUrl = config.baseUrl;
  },
  on: (event, handler) => { handlers[event] = handler; },
  setModel: async (model) => { selected.push(model.id); return true; },
};
ext(pi);
const model = (key) => models.find((m) => `${m.provider}/${m.id}` === key);
const ctx = (m) => ({ model: m, modelRegistry, ui: { notify: (n) => notes.push(n) } });
const input = async (key) => (await handlers.input({}, ctx(model(key)))).action;
"""

_PI_GUARDS = """
const proxy = model("anthropic/a"), other = model("deepseek/d");
await handlers.model_select({ model: other, previousModel: proxy }, ctx(other));
const allowed = await input("anthropic/a");
const blocked = await input("deepseek/d");
const unsupported = await input("anthropic/z");
const blockedExit = process.exitCode;
process.exitCode = 0;
let shutdown = false;
await handlers.session_start({}, { ...ctx(other), shutdown: () => { shutdown = true; } });
console.error("started");
const payload = {
  registered: registered.map(([name, config]) => [name, config.baseUrl]),
  selected, allowed, blocked, unsupported, blockedExit, notes: notes.length, shutdown,
};
console.log(JSON.stringify(payload));
"""

_PI_FOLLOW = """
const before = Object.fromEntries(models.map((m) => [`${m.provider}/${m.id}`, m.baseUrl]));
await handlers.session_start({}, { ...ctx(model("anthropic/a")), shutdown: () => notes.push("x") });
const decided = {};
for (const key of Object.keys(before)) decided[key] = await input(key);
const sent = Object.fromEntries(registered.map(([name, config]) => [name, config]));
console.log(JSON.stringify({
  decided, sent, notes, token: process.env.PRIVYX_PI_TOKEN ?? null, exit: process.exitCode,
}));
"""


def _pi_extension(tmp_path: Path, script: str, models: list[dict], **env: str) -> Any:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    extension = build_target(AgentConfig(command="pi", type="pi"))
    path = build_launch_plan(extension, [], Settings(), 1, ()).argv[1]
    return subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            _PI_STAND_IN + script,
            Path(path).as_uri(),
            json.dumps(models),
        ],
        env={"PRIVYX_PI_BASE_URL": "http://127.0.0.1:1", **env},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def _model(key: str, api: str, base: str) -> dict[str, str]:
    provider, _, model_id = key.partition("/")
    return {"provider": provider, "id": model_id, "api": api, "baseUrl": base}


def test_pi_extension_keeps_every_model_on_the_proxy(tmp_path: Path) -> None:
    import json

    models = [
        _model("anthropic/a", "anthropic-messages", "https://api.anthropic.example.com"),
        _model("deepseek/d", "openai-completions", "https://api.deepseek.example.com"),
        # Of the provider served, but its paths are no masking route.
        _model("anthropic/z", "azure-openai-responses", "https://acme.example.com/openai"),
    ]
    result = _pi_extension(tmp_path, _PI_GUARDS, models, PRIVYX_PI_PROVIDER="anthropic")
    assert json.loads(result.stdout) == {
        "registered": [["anthropic", "http://127.0.0.1:1"]],
        "selected": ["a"],  # switching away put the proxied model back
        "allowed": "continue",
        "blocked": "handled",  # a prompt never reaches a model outside the proxy
        "unsupported": "handled",
        "blockedExit": 1,
        "notes": 3,
        "shutdown": True,  # a model outside the proxy at startup ends the run
    }
    # Reported once pi has exited, past its screen, and the run fails.
    started, refusal = result.stderr.splitlines()
    assert started == "started"
    assert refusal.startswith("privyx: deepseek/d would bypass privyx")
    assert result.returncode == 1


def test_pi_extension_sends_each_provider_through_the_proxy(tmp_path: Path) -> None:
    import json

    models = [
        _model("anthropic/a", "anthropic-messages", "https://api.anthropic.example.com"),
        # One provider, a base URL per API.
        _model("mix/chat", "openai-completions", "https://mix.example.com/api/v1"),
        _model("mix/msg", "anthropic-messages", "https://mix.example.com/api"),
        _model("dupe/one", "openai-completions", "https://dupe.example.com/a"),
        _model("dupe/two", "openai-completions", "https://dupe.example.com/b"),
        _model("bed/b", "bedrock-converse-stream", "https://bedrock.example.com"),
        _model("cop/c", "openai-completions", "https://cop.example.com"),
        _model("mis/m", "mistral-conversations", "https://mistral.example.com"),
    ]
    result = _pi_extension(tmp_path, _PI_FOLLOW, models, PRIVYX_PI_TOKEN="run-secret")
    out = json.loads(result.stdout)
    assert out["decided"] == {
        "anthropic/a": "continue",
        "mix/chat": "continue",
        "mix/msg": "continue",
        "dupe/one": "handled",  # two base URLs for one API: which one is unknown
        "dupe/two": "handled",
        "bed/b": "handled",  # an API privyx cannot mask
        "cop/c": "handled",  # the login sends elsewhere
        "mis/m": "continue",  # OpenAI's Chat Completions
    }
    upstreams = {name: c["headers"]["x-privyx-upstreams"] for name, c in out["sent"].items()}
    assert {name: json.loads(value) for name, value in upstreams.items()} == {
        "anthropic": {"anthropic": "https://api.anthropic.example.com"},
        "mix": {
            "openai": "https://mix.example.com/api/v1",
            "anthropic": "https://mix.example.com/api",
        },
        "dupe": {},
        "cop": {"openai": "https://cop.example.com"},
        "mis": {"openai": "https://mistral.example.com"},
    }
    assert {c["headers"]["x-privyx-run-token"] for c in out["sent"].values()} == {"run-secret"}
    assert {c["baseUrl"] for c in out["sent"].values()} == {"http://127.0.0.1:1"}
    assert out["token"] is None  # not passed on to the commands pi runs
    assert "x" not in out["notes"]  # the starting model is served: no shutdown
    assert [n.split(" ")[1] for n in out["notes"]] == ["dupe/one", "dupe/two", "bed/b", "cop/c"]
    assert out["exit"] == 1


# pi's /new, /resume, /fork, and /reload load the extension again in the same
# process. On a reload its models already point at the proxy; a new session
# reads them from models.json again.
_PI_RELOAD = """
const start = () =>
  handlers.session_start({}, { ...ctx(model("anthropic/a")), shutdown: () => notes.push("x") });
const load = async () => {
  registered.length = 0;
  ext(pi);
  await start();
  const sent = Object.fromEntries(registered);
  const decided = {};
  for (const m of models) decided[`${m.provider}/${m.id}`] = await input(`${m.provider}/${m.id}`);
  return { decided, sent };
};
await start();
const reload = await load();
models.splice(
  0,
  models.length,
  { provider: "anthropic", id: "a", api: "anthropic-messages", baseUrl: "https://new.example.com" },
  { provider: "acme", id: "m", api: "openai-completions", baseUrl: "https://acme.example.com/v1" },
);
const renew = await load();
console.log(JSON.stringify({ reload, renew, notes, token: process.env.PRIVYX_PI_TOKEN ?? null }));
"""


def test_pi_extension_keeps_the_proxy_when_pi_loads_it_again(tmp_path: Path) -> None:
    import json

    models = [_model("anthropic/a", "anthropic-messages", "https://api.anthropic.example.com")]
    result = _pi_extension(tmp_path, _PI_RELOAD, models, PRIVYX_PI_TOKEN="run-secret")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["notes"] == []  # no shutdown, no refusal
    assert out["token"] is None  # still not passed on to the commands pi runs

    def upstreams(load: dict) -> dict:
        sent = load["sent"]
        assert {c["headers"]["x-privyx-run-token"] for c in sent.values()} == {"run-secret"}
        return {name: json.loads(c["headers"]["x-privyx-upstreams"]) for name, c in sent.items()}

    # A reload: the models already point at the proxy, the earlier upstream stays.
    assert out["reload"]["decided"] == {"anthropic/a": "continue"}
    assert upstreams(out["reload"]) == {"anthropic": {"anthropic": "https://api.anthropic.example.com"}}
    # A new session: a changed base URL and a new provider are followed.
    assert out["renew"]["decided"] == {"anthropic/a": "continue", "acme/m": "continue"}
    assert upstreams(out["renew"]) == {
        "anthropic": {"anthropic": "https://new.example.com"},
        "acme": {"openai": "https://acme.example.com/v1"},
    }
