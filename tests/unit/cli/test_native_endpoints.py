"""Native destinations survive proxy injection without forwarding Privyx credentials."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.commands.run import TARGETS
from privyx.cli.launch import build_launch_plan
from privyx.cli.main import cli
from privyx.config.schema import Settings
from privyx.core.errors import ConfigError


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr("privyx.cli.claude._managed_root", lambda: tmp_path / "managed")


def write_settings(path: Path, endpoint: str = "https://router.example.com") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": endpoint}}))


@pytest.mark.parametrize(
    "header", ["x-api-key", "Authorization", "X-Goog-Api-Key", "X-Router-Auth"]
)
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_provider_headers_require_explicit_upstream(
    header: str, agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.example.com")
    target = TARGETS[agent]
    argv = [] if agent == "claude" else ["-c", 'openai_base_url="https://router.example.com/v1"']
    settings = Settings(provider={"headers": {header: "dummy-header-credential"}})
    with pytest.raises(ConfigError, match="--upstream") as error:
        build_launch_plan(target, argv, settings, 0, target.env_vars)
    assert "dummy-header-credential" not in str(error.value)
    settings.upstream_url = "https://confirmed.example.com"
    plan = build_launch_plan(target, argv, settings, 0, target.env_vars)
    assert plan.upstream == "https://confirmed.example.com"
    assert settings.provider.headers[header] == "dummy-header-credential"


@pytest.mark.parametrize("filename", ["settings.json", "settings.local.json"])
def test_claude_reads_settings_in_git_subdirectory(
    filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write_settings(tmp_path / ".claude" / filename, "https://root.example.com")
    child = tmp_path / "child"
    write_settings(child / ".claude" / filename, "https://child.example.com")
    monkeypatch.chdir(child)
    target = TARGETS["claude"]
    plan = build_launch_plan(target, [], Settings(), 0, target.env_vars)
    expected = "child" if filename == "settings.json" else "root"
    assert plan.upstream == f"https://{expected}.example.com"
    with pytest.raises(ConfigError, match="defines ANTHROPIC_BASE_URL"):
        build_launch_plan(target, ["--settings", "{}"], Settings(), 0, target.env_vars)


@pytest.mark.parametrize("source", ["shell", "env", "env_from", "user", "project", "local"])
def test_claude_endpoint_sources(
    source: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://router.example.com"
    config = tmp_path / "config.yaml"
    config.write_text("{}")
    if source == "shell":
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    elif source == "env":
        config.write_text(f"agents:\n  claude:\n    env: {{ANTHROPIC_BASE_URL: {url}}}\n")
    elif source == "env_from":
        monkeypatch.setenv("ROUTER_URL", url)
        config.write_text("agents:\n  claude:\n    env_from: {ANTHROPIC_BASE_URL: ROUTER_URL}\n")
    else:
        path = {
            "user": home / ".claude/settings.json",
            "project": tmp_path / ".claude/settings.json",
            "local": tmp_path / ".claude/settings.local.json",
        }[source]
        write_settings(path)
    result = CliRunner().invoke(cli, ["run", "-c", str(config), "--dry-run", "claude"])
    assert result.exit_code == 0, result.output
    assert f"Upstream: {url}" in result.output
    assert "Native endpoint (used):" in result.output
    if source == "env_from":
        assert "env_from ROUTER_URL" in result.output


def test_claude_settings_precedence_and_child_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://shell.example.com")
    write_settings(tmp_path / "native/settings.json", "https://user.example.com")
    write_settings(tmp_path / ".claude/settings.json", "https://project.example.com")
    write_settings(tmp_path / ".claude/settings.local.json", "https://local.example.com")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native"))
    target = TARGETS["claude"]
    plan = build_launch_plan(target, [], Settings(), 0, target.env_vars)
    assert plan.upstream == "https://local.example.com"
    plan = build_launch_plan(target, ["--setting-sources=user"], Settings(), 0, target.env_vars)
    assert plan.upstream == "https://user.example.com"


@pytest.mark.parametrize("layer", ["user", "project", "local", "argument", "managed", "drop-in"])
@pytest.mark.parametrize("form", ["json", "equals", "file"])
def test_claude_settings_conflicts_fail_before_launch(
    layer: str, form: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = {
        "user": home / ".claude/settings.json",
        "project": tmp_path / ".claude/settings.json",
        "local": tmp_path / ".claude/settings.local.json",
        "managed": tmp_path / "managed/managed-settings.json",
        "drop-in": tmp_path / "managed/managed-settings.d/policy.json",
    }
    if layer in paths:
        write_settings(paths[layer])
    value = (
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://router.example.com"}})
        if layer == "argument"
        else '{"model":"test"}'
    )
    if form == "file":
        path = tmp_path / "supplied.json"
        path.write_text(value)
        value = str(path)
    args = [f"--settings={value}"] if form == "equals" else ["--settings", value]
    monkeypatch.setattr("privyx.cli.commands.run._run_target", lambda **kw: pytest.fail("launched"))
    result = CliRunner().invoke(
        cli, ["run", "--upstream", "https://explicit.example.com", "claude", "--", *args]
    )
    assert result.exit_code == 1, result.output
    assert "ANTHROPIC_BASE_URL" in result.output
    assert "router.example.com" not in result.output


def test_claude_non_endpoint_settings_keep_environment_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.example.com")
    target = TARGETS["claude"]
    args = ["--settings", '{"model":"test"}']
    plan = build_launch_plan(target, args, Settings(), 12345, target.env_vars)
    assert plan.argv[-2:] == args
    assert plan.upstream == "https://router.example.com"
    assert plan.environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"


@pytest.mark.parametrize(
    ("agent", "variable", "path"),
    [
        ("openai", "OPENAI_BASE_URL", "/v1"),
        ("aider", "OPENAI_API_BASE", "/v1"),
        ("grok", "GROK_XAI_API_BASE_URL", "/v1"),
        ("gemini", "GOOGLE_GEMINI_BASE_URL", ""),
        ("ollama", "OLLAMA_HOST", ""),
    ],
)
def test_environment_endpoints_override_preset_defaults(
    agent: str, variable: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "https://router.example.com" + path)
    result = CliRunner().invoke(cli, ["run", "--dry-run", agent])
    assert result.exit_code == 0, result.output
    assert "Upstream: https://router.example.com" + path in result.output


@pytest.mark.parametrize("override", [["--upstream", "https://explicit.example.com"], []])
def test_native_url_is_redacted_and_explicit_upstream_wins(
    override: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://alice:secret@router.example.com?key=hidden#fragment"
    )
    result = CliRunner().invoke(cli, ["run", "--dry-run", *override, "claude"])
    assert result.exit_code == 0, result.output
    assert "secret" not in result.output and "hidden" not in result.output
    assert f"Native endpoint ({'overridden' if override else 'used'}):" in result.output


def test_native_endpoint_refuses_privyx_key_and_mismatched_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = TARGETS["claude"]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.example.com")
    settings = Settings(provider={"type": "anthropic", "api_key": "dummy-privyx-key"})
    with pytest.raises(ConfigError, match="would receive Privyx's own API key"):
        build_launch_plan(target, [], settings, 0, target.env_vars)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://alice:secret@router.example.com/api")
    with pytest.raises(ConfigError, match="base_path") as error:
        build_launch_plan(target, [], Settings(), 0, target.env_vars)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "setting",
    [
        "upstream_url: https://explicit.example.com",
        "provider: {base_url: https://explicit.example.com}",
        "agents:\n  claude:\n    upstream: https://explicit.example.com",
    ],
)
def test_config_upstreams_win_over_native_environment(
    setting: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://native.example.com")
    config = tmp_path / "explicit.yaml"
    config.write_text(setting)
    result = CliRunner().invoke(cli, ["run", "-c", str(config), "--dry-run", "claude"])
    assert result.exit_code == 0, result.output
    assert "Upstream: https://explicit.example.com" in result.output
    assert "Native endpoint (overridden): https://native.example.com" in result.output


def test_ollama_child_host_wins_over_shell_and_preserves_explicit_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "shell.example.com")
    monkeypatch.setenv("AGENT_HOST", "child.example.com")
    config = tmp_path / "ollama.yaml"
    config.write_text("agents:\n  ollama:\n    env_from: {OLLAMA_HOST: AGENT_HOST}\n")
    args = ["run", "-c", str(config), "--dry-run"]
    result = CliRunner().invoke(cli, [*args, "ollama:test"])
    assert result.exit_code == 0, result.output
    assert "Upstream: http://child.example.com:11434" in result.output
    config.write_text(config.read_text() + "upstream_url: https://explicit.example.com\n")
    result = CliRunner().invoke(cli, [*args, "ollama:test"])
    assert result.exit_code == 0, result.output
    assert "Upstream: https://explicit.example.com" in result.output


def test_profile_settings_conflict_is_not_hidden_by_later_settings(tmp_path: Path) -> None:
    config = tmp_path / "profile.yaml"
    config.write_text(
        'agents:\n  claude:\n    args: [--settings, \'{"env":{"ANTHROPIC_BASE_URL":"https://router.example.com"}}\']\n'
    )
    result = CliRunner().invoke(
        cli, ["run", "-c", str(config), "--dry-run", "claude", "--", "--settings={}"]
    )
    assert result.exit_code == 1
    assert "defines ANTHROPIC_BASE_URL" in result.output


@pytest.mark.parametrize("variable", ["CODE_ASSIST_ENDPOINT", "GOOGLE_VERTEX_BASE_URL"])
def test_gemini_login_traps_are_not_native_destinations(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "https://unused.example.com")
    result = CliRunner().invoke(cli, ["run", "--dry-run", "gemini"])
    assert result.exit_code == 0, result.output
    assert "unused.example.com" not in result.output


def test_invalid_ollama_endpoint_does_not_print_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "https://alice:secret@router.example.com:bad")
    result = CliRunner().invoke(cli, ["run", "--dry-run", "ollama"])
    assert result.exit_code == 1
    assert "OLLAMA_HOST is not a valid address" in result.output
    assert "secret" not in result.output
