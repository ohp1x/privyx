"""Agent profiles merge within each trusted configuration layer."""

from __future__ import annotations

import json
import os
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


def test_colon_profile_inherits_its_base(tmp_path: Path) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "claude:work": {"upstream": "https://work.example"},
                    "claude:work-fast": {
                        "extends": "claude:work",
                        "args": ["--fast"],
                    },
                    "foo:bar": {"command": "foo-tool"},
                }
            }
        )
    )
    # claude:work has neither command nor extends, yet becomes a claude variant.
    work = load_config(config, agent="claude:work").agents["claude:work"]
    assert work.command == "claude"
    assert work.adapter == "claude"
    assert work.upstream == "https://work.example"
    # An explicit extends is kept as written, including one naming a : profile.
    fast = load_config(config, agent="claude:work-fast").agents["claude:work-fast"]
    assert fast.command == "claude"
    assert fast.upstream == "https://work.example"
    assert fast.args == ["--fast"]
    # An unknown base is no one's variant: the profile stands on its own.
    assert load_config(config, agent="foo:bar").agents["foo:bar"].command == "foo-tool"


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


def test_inheritance_resolves_final_parent_and_preserves_child_origins(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    global_file = home / ".privyx/config.yaml"
    global_file.parent.mkdir()
    global_file.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "work": {"extends": "claude", "env": {"CHILD": "child", "SHARED": "child"}},
                    "claude": {"args": ["--verbose"], "env": {"PARENT": "old", "SHARED": "parent"}},
                }
            }
        )
    )
    project = tmp_path / "privyx.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "claude": {
                        "command": "company-wrapper",
                        "upstream": "https://project.example",
                        "env": {"PARENT": "new"},
                    }
                }
            }
        )
    )
    trust(project)
    sources: dict[str, str] = {}
    settings = load_config(agent="work", sources=sources)
    profile = settings.agents["work"]
    assert profile.command == "company-wrapper"
    assert profile.adapter == "claude"
    assert profile.provider == "anthropic"
    assert profile.env_vars == ["ANTHROPIC_BASE_URL"]
    assert profile.args == ["--verbose"]
    assert profile.env == {"PARENT": "new", "SHARED": "child", "CHILD": "child"}
    assert settings.upstream_url == "https://project.example"
    assert sources["agents.work.command"] == str(project)
    assert sources["agents.work.env.PARENT"] == str(project)
    assert sources["agents.work.env.SHARED"] == str(global_file)
    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", "https://environment.example")
    monkeypatch.setenv("PRIVYX_PROVIDER", "openai")
    assert load_config(agent="work").upstream_url == "https://environment.example"
    assert load_config(agent="work").provider.type == "openai"


def test_child_replaces_lists_and_can_change_env_source(tmp_path: Path) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "parent": {
                        "extends": "claude",
                        "args": ["--verbose"],
                        "env": {"SWITCH_TO_REF": "old"},
                        "env_from": {"SWITCH_TO_VALUE": "OLD_REF"},
                    },
                    "child": {
                        "extends": "parent",
                        "args": [],
                        "env_vars": ["CUSTOM_BASE_URL"],
                        "env": {"SWITCH_TO_VALUE": "new"},
                        "env_from": {"SWITCH_TO_REF": "NEW_REF"},
                    },
                }
            }
        )
    )
    profile = load_config(config).agents["child"]
    assert profile.args == []
    assert profile.env_vars == ["CUSTOM_BASE_URL"]
    assert profile.env == {"SWITCH_TO_VALUE": "new"}
    assert profile.env_from == {"SWITCH_TO_REF": "NEW_REF"}


@pytest.mark.parametrize(
    ("profiles", "message"),
    [
        ({"a": {"extends": "absent"}}, "extends unknown profile"),
        ({"a": {"extends": "a"}}, "inheritance cycle"),
        ({"a": {"extends": "b"}, "b": {"extends": "a"}}, "inheritance cycle"),
        ({"a": {"extends": []}}, "extends"),
        ({"a": {"extends": ""}}, "extends"),
        ({"a": {"extends": "claude", "adapter": "missing"}}, "adapter"),
        ({"a": {"extends": "claude", "args": "--verbose"}}, "args"),
        ({"a": {"extends": "claude", "args": ["bad\x00argument"]}}, "args"),
        ({"a": {"extends": "claude", "env": {"BAD=NAME": "x"}}}, "env"),
        ({"a": {"extends": "claude", "env": {"FLAG": True}}}, "env"),
        ({"a": {"extends": "claude", "env": {"FLAG": "bad\x00value"}}}, "env"),
        ({"a": {"extends": "claude", "env_from": {"FLAG": "BAD=REF"}}}, "env_from"),
        (
            {"a": {"extends": "claude", "env": {"SAME": "x"}, "env_from": {"SAME": "REF"}}},
            "same destination",
        ),
    ],
)
def test_invalid_inheritance_and_launch_fields_fail(
    tmp_path: Path, profiles: dict[str, object], message: str
) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump({"agents": profiles}))
    with pytest.raises(ConfigError, match=message):
        load_config(config)


def test_deep_inheritance_chain_does_not_recurse(tmp_path: Path) -> None:
    profiles = {f"p{i}": {"extends": f"p{i - 1}"} for i in range(1, 1100)}
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump({"agents": {"p0": {"extends": "claude"}, **profiles}}))
    assert load_config(config).agents["p1099"].adapter == "claude"


def test_mapping_cannot_silently_override_argument_list_across_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "profiles.yaml"
    config.write_text("agents:\n  claude:\n    args: [--verbose]\n")
    monkeypatch.setenv("PRIVYX_AGENTS", '{"claude":{"args":{"bad":"shape"}}}')
    with pytest.raises(ConfigError, match="args"):
        load_config(config)


def test_child_transport_override_survives_a_later_parent_update(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    global_file = home / ".privyx/config.yaml"
    global_file.parent.mkdir()
    global_file.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "work": {
                        "extends": "claude",
                        "provider": "openai",
                        "upstream": "https://child.example",
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
                    "claude": {
                        "provider": "generic",
                        "upstream": "https://parent.example",
                    }
                }
            }
        )
    )
    trust(project)
    sources: dict[str, str] = {}
    settings = load_config(agent="work", sources=sources)
    assert settings.provider.type == "openai"
    assert settings.upstream_url == "https://child.example"
    assert sources["upstream_url"] == str(global_file)

    # A later common override still wins over the selected profile's origin.
    project.write_text("provider:\n  base_url: https://common.example\n")
    trust(project)
    assert load_config(agent="work").upstream_url == "https://common.example"


def test_profile_privyx_settings_sit_between_the_environment_and_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "operator": {"type": "redact"},
                "agents": {
                    "claude:strict": {
                        "privyx": {
                            "operator": {"type": "hash"},
                            "policy": {"type": "strict"},
                            "session": {"strategy": "conversation"},
                        },
                    },
                    "claude:stricter": {
                        "extends": "claude:strict",
                        "privyx": {"policy": {"allowed": ["EMAIL"]}},
                    },
                },
            }
        )
    )
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "client")
    sources: dict[str, str] = {}

    strict = load_config(config, agent="claude:strict", sources=sources)
    stricter = load_config(config, agent="claude:stricter")
    flag = load_config(config, agent="claude:strict", extra={"operator": {"type": "redact"}})
    plain = load_config(config, agent="claude")

    assert (strict.operator.type, strict.policy.type) == ("hash", "strict")
    assert strict.session.strategy == "conversation"  # over the environment
    assert sources["operator.type"] == "agents.claude:strict.privyx"
    # Maps merge with the parent's, as in `extends`.
    assert (stricter.policy.type, stricter.policy.allowed) == ("strict", ["EMAIL"])
    assert flag.operator.type == "redact"  # a flag still wins
    assert (plain.operator.type, plain.session.strategy) == ("redact", "client")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"detecter": {}}, "'agents.x.privyx.detecter' (did you mean 'detector'?)"),
        ({"operator": {"type": "hash", "lenght": 4}}, "'agents.x.privyx.operator.lenght'"),
        ({"agents": {}}, "'agents.x.privyx.agents'"),
    ],
)
def test_profile_privyx_settings_are_checked_like_a_config_file(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(yaml.safe_dump({"agents": {"x": {"command": "x", "privyx": override}}}))

    # Checked whichever profile runs, so a typo shows before it is needed.
    with pytest.raises(ConfigError, match="unknown setting") as error:
        load_config(config, agent="claude")
    assert message in str(error.value)


def test_a_key_given_twice_is_warned_about_and_the_last_one_still_wins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(
        "agents:\n"
        "  claude:work:\n"
        "    upstream: https://one.example\n"
        "  claude:work:\n"
        "    upstream: https://two.example\n"
    )

    settings = load_config(config)
    load_config(config)

    assert settings.agents["claude:work"].upstream == "https://two.example"
    warnings = [r.getMessage() for r in caplog.records if "given twice" in r.getMessage()]
    assert len(warnings) == 1  # once, however often the file is read
    assert "line 4: key 'claude:work'" in warnings[0]
    assert "two.example" not in warnings[0]


def test_a_merge_key_may_be_overridden(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = tmp_path / "named.yaml"
    config.write_text(
        "base: &base {command: tool, upstream: 'https://one.example'}\n"
        "agents:\n"
        "  x:\n"
        "    <<: *base\n"
        "    upstream: https://two.example\n"
    )
    with pytest.raises(ConfigError, match="unknown setting 'base'"):
        load_config(config)  # parsed fine: the anchor key is the only complaint
    assert not [r for r in caplog.records if "given twice" in r.getMessage()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize(
    ("env", "mode", "warned"),
    [
        ({"ANTHROPIC_AUTH_TOKEN": "sk-" + "ant-abc123"}, 0o644, True),
        ({"ANTHROPIC_AUTH_TOKEN": "sk-" + "ant-abc123"}, 0o600, False),
        ({"ANTHROPIC_AUTH_TOKEN": "${WORK_TOKEN}"}, 0o644, False),
        ({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, 0o644, False),
    ],
)
def test_a_readable_file_with_a_credential_is_warned_about(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    mode: int,
    warned: bool,
) -> None:
    monkeypatch.setenv("WORK_TOKEN", "x")
    config = tmp_path / "named.yaml"
    config.write_text(yaml.safe_dump({"agents": {"claude:work": {"env": env}}}))
    config.chmod(mode)

    load_config(config)

    messages = [r.getMessage() for r in caplog.records if "other users can read" in r.getMessage()]
    assert bool(messages) is warned
    for message in messages:
        assert "agents.claude:work.env.ANTHROPIC_AUTH_TOKEN" in message
        assert "abc123" not in message
