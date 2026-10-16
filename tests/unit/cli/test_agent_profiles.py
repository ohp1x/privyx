"""Launch-plan inspection uses the same profile resolution as execution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.config.redact import redact_argv


def test_dry_run_is_side_effect_free(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "work": {
                        "command": "not-installed",
                        "provider": "openai",
                        "upstream": "https://router.example/base",
                        "base_path": "/v1",
                        "env_vars": ["WORK_BASE_URL"],
                    }
                },
                # A dry run must not import this, or open any of these paths.
                "plugins": {"paths": ["/does-not-exist/plugin.py"]},
                "log_file": str(tmp_path / "debug.log"),
                "audit": {"path": str(tmp_path / "audit.log")},
                "vault": {"type": "sqlite", "dsn": f"sqlite+aiosqlite:///{tmp_path}/vault.db"},
            }
        )
    )
    import privyx.cli.commands.run as run_module

    forbidden = Mock(side_effect=AssertionError("dry run must not start anything"))
    monkeypatch.setattr(run_module, "_run_target", forbidden)
    monkeypatch.setattr(run_module, "_listen", forbidden)
    monkeypatch.setattr("privyx.security.keys.read_or_create_anchor_secret", forbidden)
    before = set(tmp_path.rglob("*")) | set(home.rglob("*"))
    result = CliRunner().invoke(
        cli, ["run", "-c", str(config), "--dry-run", "work", "--", "--flag", "value with spaces"]
    )
    assert result.exit_code == 0, result.output
    assert "Command: not-installed --flag 'value with spaces'" in result.output
    assert "WORK_BASE_URL=http://127.0.0.1:<auto>/v1" in result.output
    assert "→ https://router.example" in result.output
    assert f"Upstream: https://router.example/base [{config}]" in result.output
    assert "Anchor: auto-provision on launch" in result.output
    assert before == set(tmp_path.rglob("*")) | set(home.rglob("*"))
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    ("name", "override"),
    [
        ("claude", "--settings"),
        ("codex", "openai_base_url="),
        ("aider", "OPENAI_API_BASE"),
        ("openai", "OPENAI_BASE_URL"),
    ],
)
def test_dry_run_preserves_builtin_wiring(name: str, override: str) -> None:
    result = CliRunner().invoke(cli, ["run", "--dry-run", "--port", "43210", name])
    assert result.exit_code == 0, result.output
    assert override in result.output
    assert "http://127.0.0.1:43210" in result.output
    assert "Source run.port: CLI" in result.output


def test_profile_alias_keeps_command_adapter(tmp_path: Path) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text(
        "agents:\n  work:\n    command: claude\n    provider: anthropic\n"
        "    env_vars: [ANTHROPIC_BASE_URL]\n"
    )
    result = CliRunner().invoke(cli, ["run", "-c", str(config), "--dry-run", "work"])
    assert result.exit_code == 0, result.output
    assert "Command: claude --settings" in result.output


def test_cli_overrides_profile_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text("agents:\n  codex:\n    upstream: https://file.example\n")
    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", "https://env.example")
    monkeypatch.setenv("PRIVYX_PROVIDER", "anthropic")
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "-c",
            str(config),
            "--dry-run",
            "codex",
            "--provider",
            "openai",
            "--upstream",
            "https://cli.example",
            "--env-var",
            "EXTRA_BASE_URL",
            "--session-strategy",
            "client",
            "--no-anchor",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Provider: openai [CLI]" in result.output
    assert "Upstream: https://cli.example [CLI]" in result.output
    assert "Session: client [CLI]" in result.output
    assert "Anchor: disabled" in result.output
    assert "EXTRA_BASE_URL=http://127.0.0.1:<auto>/v1 [CLI]" in result.output


def test_dry_run_masks_credentials(tmp_path: Path) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "aider": {
                        "upstream": "https://user:url-password-value@router.example?key=query-secret"
                    }
                },
                "provider": {
                    "api_key": "provider-secret",
                    "headers": {"Authorization": "header-secret"},
                },
                "anchor": {"secret": "anchor-secret"},
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "-c",
            str(config),
            "--dry-run",
            "aider",
            "--",
            "--api-key",
            "argument-secret",
            "--token=token-secret",
            "-c",
            'provider.api_key="config-secret"',
            "--settings",
            json.dumps({"env": {"AUTH_TOKEN": "json-secret="}}),
        ],
    )
    assert result.exit_code == 0, result.output
    for secret in (
        "url-password-value",
        "query-secret",
        "provider-secret",
        "header-secret",
        "anchor-secret",
        "argument-secret",
        "token-secret",
        "config-secret",
        "json-secret",
    ):
        assert secret not in result.output
    assert "router.example" in result.output
    shown = CliRunner().invoke(cli, ["config", "--show", "-c", str(config)])
    assert shown.exit_code == 0
    assert "url-password-value" not in shown.output
    assert "query-secret" not in shown.output


def test_redacted_argv_preserves_actual_arguments() -> None:
    argv = [
        "tool",
        "--api-key",
        "raw-key",
        "--base-url=https://token@router?key=value",
        'provider.base_url="https://user:pw@router?key=secret"',
    ]
    shown = redact_argv(argv)
    assert argv[2] == "raw-key"
    assert all(secret not in " ".join(shown) for secret in ("raw-key", "token", "pw", "secret"))


def test_command_preview_masks_key_flags_and_handles_invalid_urls() -> None:
    assert redact_argv(["tool", "--key", "private", "--authorization=BearerValue"]) == [
        "tool",
        "--key",
        "***",
        "--authorization=***",
    ]
    assert redact_argv(["tool", "--base-url=http://[malformed"]) == [
        "tool",
        "--base-url=***",
    ]


def test_list_includes_profiles(tmp_path: Path) -> None:
    config = tmp_path / "custom.yaml"
    config.write_text("agents:\n  work:\n    command: work-tool\n    env_vars: [WORK_BASE_URL]\n")
    result = CliRunner().invoke(cli, ["run", "-c", str(config), "--list"])
    assert result.exit_code == 0, result.output
    assert "work" in result.output and "WORK_BASE_URL" in result.output
    assert "claude" in result.output
