"""Agent profiles merge within each trusted configuration layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from privyx.config.loader import load_config, trust
from privyx.core.errors import ConfigError


def test_profile_layers_and_sources(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    global_file = home / ".privyx/config.yaml"
    global_file.parent.mkdir()
    global_file.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "codex": {
                        "command": "codex-work",
                        "upstream": "https://global.example",
                        "base_path": "/global",
                    }
                }
            }
        )
    )
    project = tmp_path / "privyx.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "codex": {
                        "upstream": "https://project.example",
                        "env_vars": ["PROJECT_BASE_URL"],
                    }
                }
            }
        )
    )
    trust(project)
    sources: dict[str, str] = {}
    settings = load_config(agent="codex", sources=sources)
    assert settings.agents["codex"].command == "codex-work"
    assert settings.agents["codex"].base_path == "/global"
    assert settings.agents["codex"].env_vars == ["PROJECT_BASE_URL"]
    assert settings.provider.type == "openai"
    assert settings.upstream_url == "https://project.example"
    assert sources["upstream_url"] == str(project)
    assert sources["agents.codex.command"] == str(global_file)
    assert sources["provider.type"] == "defaults"

    project.write_text("provider:\n  base_url: https://project-common.example\n")
    trust(project)
    assert load_config(agent="codex").upstream_url == "https://project-common.example"

    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", "https://env.example")
    monkeypatch.setenv("PRIVYX_PROVIDER", "anthropic")
    monkeypatch.setenv("PRIVYX_AGENTS", json.dumps({"codex": {"base_path": "/env"}}))
    settings = load_config(agent="codex", sources=sources)
    assert settings.upstream_url == "https://env.example"
    assert settings.provider.type == "anthropic"
    assert settings.agents["codex"].base_path == "/env"
    assert sources["upstream_url"] == "environment"
    settings = load_config(
        agent="codex",
        sources=sources,
        extra={
            "upstream_url": "https://cli.example",
            "provider": {"type": "generic"},
        },
    )
    assert settings.upstream_url == "https://cli.example"
    assert settings.provider.type == "generic"
    assert sources["upstream_url"] == "CLI"


def test_profile_wins_within_layer_but_later_common_settings_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "provider": {"type": "generic"},
                "upstream_url": "https://common.example",
                "agents": {"claude": {"provider": "openai", "upstream": "https://agent.example"}},
            }
        )
    )
    settings = load_config(config, agent="claude")
    assert settings.provider.type == "openai"
    assert settings.upstream_url == "https://agent.example"
    monkeypatch.setenv("PRIVYX_PROVIDER", "anthropic")
    assert load_config(config, agent="claude").provider.type == "anthropic"
    # Other commands do not select or apply agent transport settings.
    assert load_config(config).upstream_url == "https://common.example"


def test_changed_project_profile_loses_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "privyx.yaml"
    project.write_text("agents:\n  codex:\n    command: trusted-codex\n")
    assert load_config(agent="codex").agents["codex"].command == "codex"
    trust(project)
    assert load_config(agent="codex").agents["codex"].command == "trusted-codex"
    project.write_text("agents:\n  codex:\n    command: changed-codex\n")
    assert load_config(agent="codex").agents["codex"].command == "codex"


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"command": ""}, "command"),
        ({"command": "   "}, "command"),
        ({"command": "tool", "env_vars": ["BAD=NAME"]}, "env_vars"),
        ({"command": "tool", "env_vars": "BASE_URL"}, "env_vars"),
        ({"command": "tool", "base_path": "v1"}, "base_path"),
        ({"command": "tool", "base_path": "//other.example"}, "base_path"),
        ({"command": "tool", "base_path": "/v1?key=secret"}, "base_path"),
        ({"command": "tool", "upstream": "ftp://host"}, "upstream"),
        ({"command": "tool", "upstream": "https://host:99999"}, "upstream"),
        ({"command": "tool", "env_var": ["BASE_URL"]}, "did you mean 'env_vars'"),
        ({"env_vars": ["BASE_URL"]}, "command"),
    ],
)
def test_bad_profile_fails_at_startup(
    tmp_path: Path, profile: dict[str, object], message: str
) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(yaml.safe_dump({"agents": {"work": profile}}))
    with pytest.raises(ConfigError, match=message):
        load_config(config, agent="work")


@pytest.mark.parametrize("value", ["{broken", "[]", "null", '"secret"'])
def test_bad_agents_environment_is_reported_without_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PRIVYX_AGENTS", value)
    with pytest.raises(ConfigError, match="PRIVYX_AGENTS must be a JSON object"):
        load_config()


def test_named_config_ignores_other_profiles(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    global_file = home / ".privyx/config.yaml"
    global_file.parent.mkdir()
    global_file.write_text("agents:\n  codex:\n    command: global-codex\n")
    project = tmp_path / "privyx.yaml"
    project.write_text("agents:\n  codex:\n    command: project-codex\n")
    trust(project)
    named = tmp_path / "named.yaml"
    named.write_text("agents:\n  codex:\n    base_path: /named\n")
    settings = load_config(named, agent="codex")
    assert settings.agents["codex"].command == "codex"
    assert settings.agents["codex"].base_path == "/named"
