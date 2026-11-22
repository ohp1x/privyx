"""Named providers, and a credential that goes only where its provider points."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.app import read_config
from privyx.cli.main import cli
from privyx.config import trust
from privyx.core import ConfigError
from privyx.providers import check_credential, resolve_api_key, resolve_base_url

KEY = "sk-" + "ant-fake"
ROUTER = "https://router.example.com"


def _file(path: Path, data: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_named_provider_is_selected_with_its_own_key(tmp_path: Path) -> None:
    config = _file(
        tmp_path / "c.yaml",
        {
            "providers": {"work": {"type": "anthropic", "base_url": ROUTER, "api_key": KEY}},
            "provider": "work",
            "agents": {"claude:work": {"provider": "work"}},
        },
    )

    for settings in (load_config(config), load_config(config, agent="claude:work")):
        assert (settings.provider.type, resolve_base_url(settings)) == ("anthropic", ROUTER)
        assert resolve_api_key(settings) == KEY
        check_credential(settings)  # its own origin
    assert resolve_api_key(load_config(config, agent="claude")) is None  # anthropic's own


def test_an_unknown_provider_is_an_error_with_a_suggestion(tmp_path: Path) -> None:
    config = _file(tmp_path / "c.yaml", {"providers": {"work": {}}, "provider": "wrok"})

    with pytest.raises(ConfigError, match="provider 'wrok' .* is not defined .*'work'"):
        load_config(config)


def test_a_key_does_not_follow_a_profile_upstream_to_another_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_ANTHROPIC_API_KEY", KEY)
    config = _file(tmp_path / "c.yaml", {"agents": {"claude:work": {"upstream": ROUTER}}})

    settings = load_config(config, agent="claude:work")  # reading the config is fine

    assert resolve_base_url(settings) == ROUTER
    with pytest.raises(ConfigError, match="is for https://api.anthropic.com only") as error:
        check_credential(settings)
    assert KEY not in str(error.value)
    result = CliRunner().invoke(cli, ["-c", str(config), "run", "--dry-run", "claude:work"])
    assert result.exit_code == 1
    assert "would go to https://router.example.com" in result.output
    assert KEY not in result.output


def test_a_later_file_cannot_move_a_named_provider_away_from_its_key(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    user = home / ".privyx" / "config.yaml"
    user.parent.mkdir()
    _file(user, {"providers": {"work": {"base_url": ROUTER, "api_key": KEY}}, "provider": "work"})
    project = _file(
        tmp_path / "privyx.yaml", {"providers": {"work": {"base_url": "https://x.example"}}}
    )
    trust(project)

    with pytest.raises(ConfigError, match="would go to https://x.example"):
        check_credential(load_config())
    # Written beside the URL, a key goes there.
    _file(project, {"providers": {"work": {"base_url": "https://x.example", "api_key": "own"}}})
    trust(project)
    check_credential(load_config())


def test_the_environment_key_goes_to_the_environment_url_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVYX_API_KEY", KEY)
    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", ROUTER)

    settings = load_config()
    check_credential(settings)
    assert (resolve_base_url(settings), resolve_api_key(settings)) == (ROUTER, KEY)
    with pytest.raises(ConfigError, match="would go to https://elsewhere.example"):
        check_credential(load_config(extra={"upstream_url": "https://elsewhere.example"}))


@pytest.mark.parametrize("agent", ["grok", "ollama"])
@pytest.mark.parametrize(
    "variable", ["PRIVYX_API_KEY", "PRIVYX_OPENAI_API_KEY", "PRIVYX_ANTHROPIC_API_KEY"]
)
def test_a_profile_for_another_host_takes_no_key_of_privyx(
    agent: str, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, KEY)
    monkeypatch.setenv("PRIVYX_PROVIDER", "openai")  # the top's choice, not the profile's

    assert resolve_api_key(load_config(agent=agent)) is None


def test_the_old_upstream_keys_still_work_with_a_warning(tmp_path: Path) -> None:
    config = _file(
        tmp_path / "c.yaml",
        {
            "upstream_url": ROUTER,
            "provider": {"type": "openai", "openai_api_key": KEY},
            "proxy": {"timeout": 42, "forward_client_auth": False},
        },
    )
    warnings: list[str] = []

    loaded = read_config(config)
    warnings = loaded.warnings
    settings = loaded.settings

    assert resolve_base_url(settings) == ROUTER
    assert settings.providers["openai"].api_key == KEY
    assert (settings.provider.timeout, settings.provider.forward_client_auth) == (42, False)
    for old, new in (
        ("upstream_url", "provider.base_url"),
        ("provider.openai_api_key", "providers.openai.api_key"),
        ("proxy.timeout", "provider.timeout"),
        ("proxy.forward_client_auth", "provider.forward_client_auth"),
    ):
        assert f"{old!r} is deprecated; write {new!r} (removed in 0.4.0)" in warnings
    # The key is the built-in provider openai's: the provider of its own here
    # does not take it, and says so.
    assert resolve_api_key(settings) is None
    assert any("the key of provider 'openai' is not sent" in w for w in warnings)


def test_the_environment_variables_of_the_upstream_give_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "PRIVYX_UPSTREAM_URL": ROUTER,
        "PRIVYX_API_KEY": KEY,
        "PRIVYX_PROVIDER": "anthropic",
        "PRIVYX_OPENAI_API_KEY": KEY,
    }.items():
        monkeypatch.setenv(name, value)
    warnings: list[str] = []

    loaded = read_config()
    warnings = loaded.warnings
    settings = loaded.settings

    assert warnings == []
    assert (settings.provider.type, resolve_base_url(settings)) == ("anthropic", ROUTER)


def test_token_settings_an_operator_does_not_use_are_warned_about(tmp_path: Path) -> None:
    config = _file(
        tmp_path / "c.yaml",
        {"operator": {"type": "faker"}, "token": {"namespace": "ACME"}},
    )
    warnings: list[str] = []

    loaded = read_config(config)
    warnings = loaded.warnings

    assert any(
        f"'token.namespace' from {config} has no effect: operator type 'faker'" in w
        for w in warnings
    )


def test_a_preset_cannot_name_a_provider(tmp_path: Path) -> None:
    config = _file(tmp_path / "c.yaml", {"presets": {"p": {"providers": {"work": {}}}}})

    with pytest.raises(ConfigError, match="presets.p.providers"):
        load_config(config)


def test_a_common_url_beside_a_profile_still_sends_it_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _file(
        tmp_path / "c.yaml",
        {
            "provider": {"base_url": ROUTER},
            "agents": {"acme": {"command": "acme", "provider": "anthropic"}},
        },
    )

    settings = load_config(config, agent="acme")

    assert (settings.provider.type, resolve_base_url(settings)) == ("anthropic", ROUTER)
    check_credential(settings)
    monkeypatch.setenv("PRIVYX_ANTHROPIC_API_KEY", KEY)  # anthropic's key: for its own host
    with pytest.raises(ConfigError, match="would go to https://router.example.com"):
        check_credential(load_config(config, agent="acme"))


OWN = "https://own.example.com"
CLI_URL = "https://cli.example.com"


def _beside_common(path: Path, named: dict[str, object], **agent: object) -> Path:
    """Common connection settings, and profile ``p`` on provider ``work``, defined as ``named``."""
    profile = {"command": "acme", "env_vars": ["ACME_URL"], "provider": "work", **agent}
    return _file(
        path,
        {
            "provider": {"base_url": ROUTER, "forward_client_auth": False, "timeout": 42},
            "providers": {"work": {"type": "anthropic", **named}},
            "agents": {"p": profile},
        },
    )


def test_a_named_provider_keeps_its_url_and_key_beside_a_common_url(tmp_path: Path) -> None:
    config = _beside_common(tmp_path / "c.yaml", {"base_url": OWN, "api_key": KEY})

    settings = load_config(config, agent="p")

    assert (resolve_base_url(settings), resolve_api_key(settings)) == (OWN, KEY)
    check_credential(settings)


def test_a_named_provider_without_a_url_takes_the_common_one(tmp_path: Path) -> None:
    settings = load_config(_beside_common(tmp_path / "c.yaml", {"base_url": ""}), agent="p")

    assert resolve_base_url(settings) == ROUTER


def test_a_later_layers_url_still_moves_a_named_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _beside_common(tmp_path / "c.yaml", {"base_url": OWN})
    keyed = _beside_common(tmp_path / "k.yaml", {"base_url": OWN, "api_key": KEY})
    moved = {"upstream_url": CLI_URL}

    assert resolve_base_url(load_config(config, agent="p", extra=moved)) == CLI_URL
    with pytest.raises(ConfigError, match="would go to https://cli.example.com"):
        check_credential(load_config(keyed, agent="p", extra=moved))
    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", CLI_URL)
    assert resolve_base_url(load_config(config, agent="p")) == CLI_URL


@pytest.mark.parametrize(
    ("first", "second", "url"),
    [
        ({"provider": {"base_url": ROUTER}}, {"providers": {"work": {"base_url": OWN}}}, OWN),
        ({"providers": {"work": {"base_url": OWN}}}, {"provider": {"base_url": ROUTER}}, ROUTER),
    ],
    ids=["provider-later", "common-later"],
)
def test_between_files_the_later_url_wins(
    first: dict[str, object],
    second: dict[str, object],
    url: str,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    user = home / ".privyx" / "config.yaml"
    user.parent.mkdir()
    agents = {"p": {"command": "acme", "provider": "work"}}
    _file(user, {"providers": {"work": {"type": "anthropic"}}, "agents": agents, **first})
    trust(_file(tmp_path / "privyx.yaml", second))

    assert resolve_base_url(load_config(agent="p")) == url


@pytest.mark.parametrize("agent", [{}, {"provider": "anthropic"}], ids=["no-provider", "provider"])
def test_the_upstream_flag_moves_a_profiles_upstream(
    agent: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _file(
        tmp_path / "c.yaml", {"agents": {"p": {"command": "acme", "upstream": OWN, **agent}}}
    )

    assert resolve_base_url(load_config(config, agent="p")) == OWN
    moved = load_config(config, agent="p", extra={"upstream_url": CLI_URL})
    assert resolve_base_url(moved) == CLI_URL
    monkeypatch.setenv("PRIVYX_UPSTREAM_URL", ROUTER)
    assert resolve_base_url(load_config(config, agent="p")) == ROUTER


def test_a_providers_own_url_does_not_replace_a_profiles_upstream(tmp_path: Path) -> None:
    config = _beside_common(tmp_path / "c.yaml", {"base_url": OWN}, upstream=CLI_URL)

    assert resolve_base_url(load_config(config, agent="p")) == CLI_URL
