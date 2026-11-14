"""Unit tests for routing a custom Codex provider through Privyx."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.codex import custom_provider, openai_endpoint
from privyx.cli.launch import build_launch_plan, profile_target
from privyx.cli.main import cli
from privyx.config.schema import AgentConfig, Settings
from privyx.core.errors import ConfigError

ROUTER = """
model_provider = "router"
[model_providers.router]
name = "Router"
base_url = "http://router.test:20128/v1"
http_headers = { Authorization = "Bearer router-credential" }
[model_providers.other]
base_url = "http://other.test/v1"
"""


def test_openai_endpoint_uses_config_not_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ignored.example.com/v1")
    assert openai_endpoint({"OPENAI_BASE_URL": "https://ignored.example.com/v1"}, []) is None
    assert openai_endpoint({}, ["-c", 'openai_base_url="https://router.example.com/v1"']) == (
        "https://router.example.com/v1",
        "Codex config (openai_base_url)",
    )


@pytest.fixture
def codex_home(home: Path) -> Path:
    root = home / ".codex"
    root.mkdir()
    (root / "config.toml").write_text(ROUTER)
    (root / "work.config.toml").write_text('model_provider = "other"\n')
    return root


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ("router", "http://router.test:20128/v1")),
        (["-c", "model_provider=other"], ("other", "http://other.test/v1")),
        (["--config=model_provider='other'"], ("other", "http://other.test/v1")),
        (
            ["-c", 'model_providers.router.base_url="http://x.test/v1"'],
            ("router", "http://x.test/v1"),
        ),
        (["-p", "work"], ("other", "http://other.test/v1")),
        (
            ["--profile=work", "-c", "model_provider=router"],
            ("router", "http://router.test:20128/v1"),
        ),
        (["-c", "model_provider=openai"], None),
        (["-c", "model_provider=missing"], None),
        (["--", "-c", "model_provider=other"], ("router", "http://router.test:20128/v1")),
    ],
)
def test_custom_provider_follows_codex_config_layers(
    codex_home: Path, argv: list[str], expected: tuple[str, str] | None
) -> None:
    assert custom_provider({}, argv) == expected


def test_custom_provider_reads_codex_home_and_rejects_unaddressable_ids(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'model_provider = "my.router"\n[model_providers."my.router"]\nbase_url = "http://r/v1"\n'
    )
    with pytest.raises(ConfigError, match="my.router"):
        custom_provider({"CODEX_HOME": str(tmp_path)}, [])


def _dry_run(*args: str) -> str:
    result = CliRunner().invoke(cli, ["run", "--dry-run", *args])
    return result.output


def test_dry_run_routes_custom_provider_through_privyx(codex_home: Path) -> None:
    output = _dry_run("codex")
    assert "-c 'model_providers.router.base_url=\"http://127.0.0.1:<auto>/v1\"'" in output
    assert "Upstream: http://router.test:20128/v1 [Codex config (model_providers.router)]" in output
    assert "→ http://router.test:20128\n" in output


def test_dry_run_keeps_an_explicit_upstream(codex_home: Path) -> None:
    output = _dry_run("-u", "https://gateway.test", "codex")
    assert "model_providers.router.base_url" in output
    assert "Upstream: https://gateway.test [CLI]" in output


def test_custom_provider_never_receives_privyx_key(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_OPENAI_API_KEY", "privyx-key")
    output = _dry_run("codex")
    assert "would receive Privyx's own API key" in output
    assert "privyx-key" not in output


def test_custom_provider_under_another_path_is_refused(codex_home: Path) -> None:
    output = _dry_run("codex", "--", "-c", 'model_providers.router.base_url="http://r.test/api/v1"')
    assert "cannot route Codex provider 'router'" in output


@pytest.mark.parametrize("variable", ["CODEX_HOME", "HOME"])
def test_provider_detection_and_launch_share_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    root = tmp_path if variable == "CODEX_HOME" else tmp_path / ".codex"
    root.mkdir(exist_ok=True)
    (root / "config.toml").write_text(ROUTER)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv(variable, str(tmp_path / "unused"))
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_PATH", str(tmp_path / "bin"))
    target = profile_target(
        "work",
        AgentConfig(
            command="codex",
            adapter="codex",
            base_path="/v1",
            env_from={variable: "AGENT_HOME", "PATH": "AGENT_PATH"},
        ),
    )

    def detect(environment: dict[str, str], argv: list[str]) -> tuple[str, str] | None:
        assert environment[variable] == str(tmp_path)
        found = custom_provider(environment, argv)
        monkeypatch.setenv("AGENT_HOME", str(tmp_path / "changed"))
        monkeypatch.setenv("AGENT_PATH", str(tmp_path / "changed-bin"))
        return found

    monkeypatch.setattr("privyx.cli.codex.custom_provider", detect)
    plan = build_launch_plan(target, [], Settings(), 0, (), name="work")
    bound = plan.bind_port(12345)
    assert plan.upstream == "http://router.test:20128/v1"
    assert plan.sources["run.upstream"] == "Codex config (model_providers.router)"
    assert 'model_providers.router.base_url="http://127.0.0.1:12345/v1"' in bound.argv
    assert bound.environment[variable] == str(tmp_path)
    assert bound.environment["PATH"] == str(tmp_path / "bin")
    assert "env_from AGENT_HOME" in bound.env_sources[variable]
