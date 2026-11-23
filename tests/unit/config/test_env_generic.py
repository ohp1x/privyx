"""``PRIVYX_<PATH>``: a variable for every setting, beside the names of earlier versions."""

from __future__ import annotations

from pathlib import Path

import pytest

from privyx import load_config
from privyx.app import read_config
from privyx.core import ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIVYX_CONFIG", raising=False)
    monkeypatch.setenv("PRIVYX_NO_DISCOVERY", "1")


def test_a_path_with_double_underscores_sets_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_SERVER__PORT", "8001")
    monkeypatch.setenv("PRIVYX_FEATURES__DIRECTIVE", "true")
    monkeypatch.setenv("PRIVYX_LOG__LEVEL", "debug")
    monkeypatch.setenv("PRIVYX_VAULT__TTL", "60")
    sources: dict[str, str] = {}

    loaded = read_config()
    sources = loaded.sources
    settings = loaded.settings

    assert settings.server.port == 8001  # read by the type of the setting
    assert settings.features["directive"].enabled is True
    assert settings.log.level == "debug"
    assert settings.vault.model_dump()["ttl"] == 60  # an option of the vault's type
    assert sources["server.port"] == "environment"


def test_a_name_matches_whatever_its_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_server__Port", "8002")
    assert load_config().server.port == 8002


def test_text_stays_text_where_text_is_wanted(monkeypatch: pytest.MonkeyPatch) -> None:
    # YAML would read NO as false.
    monkeypatch.setenv("PRIVYX_TOKEN__NAMESPACE", "NO")
    assert load_config().token.namespace == "NO"


def test_a_list_or_a_mapping_is_read_as_yaml_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_DETECTORS__REGEX__TERMS", "{PERSON: [john smith, alice]}")
    monkeypatch.setenv("PRIVYX_PLUGINS__MODULES", "[]")

    settings = load_config()

    assert settings.detectors["regex"].model_dump()["terms"] == {"PERSON": ["john smith", "alice"]}
    assert settings.plugins.modules == []


def test_an_entry_the_config_has_is_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("detectors:\n  presidio:\n    language: en\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_DETECTORS__PRESIDIO__LANGUAGE", "id")
    monkeypatch.setenv("PRIVYX_AGENTS__CLAUDE__COMMAND", "my-claude")

    settings = load_config(config)

    assert settings.detectors["presidio"].model_dump()["language"] == "id"
    assert settings.agents["claude"].command == "my-claude"


@pytest.mark.parametrize(
    ("variable", "message"),
    [
        ("PRIVYX_SERVR__PORT", "no setting 'servr' (did you mean 'server'?)"),
        ("PRIVYX_SERVER__PROT", "no setting 'server.prot' (did you mean 'server.port'?)"),
        (
            "PRIVYX_DETECTORS__PRESIDIO__LANGUAGE",
            "'detectors' has no entry 'presidio'; add it in a config file",
        ),
        ("PRIVYX_SERVER__PORT__X", "'server.port' is a value"),
    ],
)
def test_a_name_that_is_no_setting_is_refused(
    monkeypatch: pytest.MonkeyPatch, variable: str, message: str
) -> None:
    monkeypatch.setenv(variable, "sk-" + "secret-value")

    with pytest.raises(ConfigError, match=f"{variable} names no setting") as caught:
        load_config()

    assert message in str(caught.value)
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("alias", "generic", "same"),
    [
        (("PRIVYX_PORT", "8001"), ("PRIVYX_SERVER__PORT", "8001"), True),
        (("PRIVYX_PORT", "8001"), ("PRIVYX_SERVER__PORT", "8002"), False),
        (("PRIVYX_AUDIT_ENABLED", "1"), ("PRIVYX_FEATURES__AUDIT__ENABLED", "true"), True),
        (("PRIVYX_AUDIT_ENABLED", "0"), ("PRIVYX_FEATURES__AUDIT__ENABLED", "true"), False),
    ],
)
def test_an_alias_and_the_generic_name_must_agree(
    monkeypatch: pytest.MonkeyPatch,
    alias: tuple[str, str],
    generic: tuple[str, str],
    same: bool,
) -> None:
    monkeypatch.setenv(*alias)
    monkeypatch.setenv(*generic)

    if same:
        load_config()
        return
    with pytest.raises(ConfigError, match=f"{alias[0]} and {generic[0]} set .* different values"):
        load_config()


def test_the_provider_url_follows_the_provider_the_environment_picks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVYX_PROVIDER", "openai")
    monkeypatch.setenv("PRIVYX_PROVIDER__BASE_URL", "https://router.example.com/v1")
    monkeypatch.setenv("PRIVYX_PROVIDER__TIMEOUT", "30")

    settings = load_config()

    assert settings.provider.type == "openai"
    assert settings.provider.base_url == "https://router.example.com/v1"
    assert settings.provider.timeout == 30


def test_harvest_takes_a_name_without_double_underscores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_HARVEST", "true")
    monkeypatch.setenv("PRIVYX_TEST_SOMETHING_ELSE", "x")  # not a setting: left alone
    assert load_config().harvest is True


@pytest.mark.parametrize(
    ("env", "section", "key", "value"),
    [
        (
            {"PRIVYX_VAULT__TYPE": "sqlite", "PRIVYX_VAULT__DSN": "sqlite+aiosqlite:///x.db"},
            "vault",
            "dsn",
            "sqlite+aiosqlite:///x.db",
        ),
        (
            {"PRIVYX_VAULT": "sqlite", "PRIVYX_VAULT__DSN": "sqlite+aiosqlite:///x.db"},
            "vault",
            "dsn",
            "sqlite+aiosqlite:///x.db",
        ),
        (
            {"PRIVYX_VAULT": "redis", "PRIVYX_VAULT__REDIS_URL": "redis://localhost:6379/0"},
            "vault",
            "redis_url",
            "redis://localhost:6379/0",
        ),
        (
            {"PRIVYX_OPERATOR__TYPE": "redact", "PRIVYX_OPERATOR__TEXT": "[HIDDEN]"},
            "operator",
            "text",
            "[HIDDEN]",
        ),
    ],
)
def test_an_option_of_a_type_the_environment_sets_is_a_setting(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], section: str, key: str, value: str
) -> None:
    for name, text in env.items():
        monkeypatch.setenv(name, text)

    assert getattr(load_config(), section).model_dump()[key] == value


def test_an_option_the_type_from_the_environment_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVYX_VAULT", "memory")
    monkeypatch.setenv("PRIVYX_VAULT__DSN", "sqlite+aiosqlite:///x.db")

    with pytest.raises(ConfigError, match="PRIVYX_VAULT__DSN names no setting"):
        load_config()


def test_both_names_of_a_type_must_agree_with_its_option_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVYX_VAULT", "memory")
    monkeypatch.setenv("PRIVYX_VAULT__TYPE", "sqlite")
    monkeypatch.setenv("PRIVYX_VAULT__DSN", "sqlite+aiosqlite:///x.db")

    with pytest.raises(ConfigError, match="PRIVYX_VAULT and PRIVYX_VAULT__TYPE"):
        load_config()


def test_a_type_from_the_environment_keeps_a_reference_in_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "agents:\n  other:\n    command: acme\n    env_vars: [ACME_URL]\n"
        "    env: {ACME_KEY: '${NOT_SET_HERE}'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVYX_VAULT__TYPE", "sqlite")
    monkeypatch.setenv("PRIVYX_VAULT__DSN", "sqlite+aiosqlite:///x.db")

    assert load_config(path).vault.model_dump()["dsn"] == "sqlite+aiosqlite:///x.db"
