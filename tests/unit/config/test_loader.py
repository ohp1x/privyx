"""Config precedence, focused on ``base_extra`` (:func:`privyx.config.loader.load_config`).

Precedence, low → high: defaults < base_extra < YAML < env < extra.  ``base_extra``
is the channel ``privyx run`` uses for defaults the user must still be able to
override (e.g. a per-conversation session).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from privyx.config.loader import active_config_paths, find_local_configs, load_config, trust
from privyx.core.builder import build_detector_from
from privyx.core.context import Context
from privyx.core.errors import ConfigError

_CONVO = {"session": {"strategy": "conversation"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the developer's own PRIVYX_* shell vars out of these precedence tests.
    monkeypatch.delenv("PRIVYX_SESSION_STRATEGY", raising=False)
    monkeypatch.delenv("PRIVYX_CONFIG", raising=False)


def test_base_extra_applies_when_nothing_overrides() -> None:
    assert load_config(base_extra=_CONVO).session.strategy == "conversation"


def test_yaml_overrides_base_extra(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("session:\n  strategy: client\n", encoding="utf-8")
    assert load_config(cfg, base_extra=_CONVO).session.strategy == "client"


def test_env_overrides_base_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "client")
    assert load_config(base_extra=_CONVO).session.strategy == "client"


def test_env_sets_the_vault_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_VAULT_TTL", "604800")
    assert load_config().vault.ttl == 604800


@pytest.mark.parametrize("value", ["0", "7d"])
def test_an_invalid_env_vault_ttl_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Ignoring it would leave sessions, with their original values, to never expire.
    monkeypatch.setenv("PRIVYX_VAULT_TTL", value)
    with pytest.raises(ConfigError, match="vault.ttl"):
        load_config()


@pytest.mark.parametrize("value", ["80a", "70000", "-1"])
def test_an_invalid_env_port_fails_at_load(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PRIVYX_PORT", value)
    with pytest.raises(ConfigError, match="port"):
        load_config()


@pytest.mark.parametrize("var", ["PRIVYX_AUDIT_ENABLED", "PRIVYX_DETECTOR_CACHE"])
@pytest.mark.parametrize("value", ["treu", "Y", "enabled"])
def test_an_invalid_env_boolean_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    # Read as false, a typo in PRIVYX_AUDIT_ENABLED would turn the audit trail off.
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=var) as exc:
        load_config()
    assert repr(value) not in str(exc.value)


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), (" On ", True), ("off", False), ("0", False)]
)
def test_env_booleans(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("PRIVYX_AUDIT_ENABLED", value)
    assert load_config().audit.enabled is expected


def test_extra_overrides_base_extra() -> None:
    settings = load_config(extra={"session": {"strategy": "ephemeral"}}, base_extra=_CONVO)
    assert settings.session.strategy == "ephemeral"


def test_base_extra_leaves_unrelated_defaults_intact(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("policy:\n  type: strict\n", encoding="utf-8")
    settings = load_config(cfg, base_extra=_CONVO)
    assert settings.session.strategy == "conversation"
    assert settings.policy.type == "strict"
    assert settings.operator.type == "pseudonym"  # untouched default


def test_detector_accepts_a_list(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "detector:\n  - type: regex\n  - type: yaml\n    patterns: {CODE: 'X\\d+'}\n",
        encoding="utf-8",
    )
    settings = load_config(cfg)
    assert isinstance(settings.detector, list)
    assert settings.detector_type == "regex+yaml"


async def test_yaml_detector_sees_only_its_own_patterns(tmp_path: Path) -> None:
    # The built-in patterns in the defaults used to be merged into it.
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detector:\n  type: yaml\n  patterns: {CODE: 'X\\d+'}\n", encoding="utf-8")
    detector = build_detector_from(load_config(cfg))
    detection = await detector.detect("X12 mail a@b.io", Context())
    assert [span.entity_type for span in detection.spans] == ["CODE"]


async def test_regex_detector_keeps_the_builtin_patterns(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detector:\n  type: regex\n  patterns: {CODE: 'X\\d+'}\n", encoding="utf-8")
    detector = build_detector_from(load_config(cfg))
    detection = await detector.detect("X12 mail a@b.io", Context())
    assert sorted(span.entity_type for span in detection.spans) == ["CODE", "EMAIL"]


def test_env_detector_cache_applies_to_each_listed_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to replace the whole list with one default detector, so a
    # configured LLM or Presidio detector silently stopped scanning.
    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "detector:\n"
        "  - type: regex\n    terms: {PROJECT: [bluebird]}\n"
        "  - type: yaml\n    patterns: {CODE: 'X\\d+'}\n",
        encoding="utf-8",
    )
    settings = load_config(cfg)
    assert isinstance(settings.detector, list)
    assert settings.detector_type == "regex+yaml"
    assert settings.detector[0].terms == {"PROJECT": ["bluebird"]}
    assert [d.cache.enabled for d in settings.detector] == [False, False]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("detector:\n  term: {PERSON: [Zed Quill]}\n", "'detector.term' (did you mean 'terms'?)"),
        ("detectors:\n  - type: regex\n", "'detectors' (did you mean 'detector'?)"),
        (
            "detector:\n  - type: regex\n  - type: yaml\n    patern: {CODE: X}\n",
            "'detector[1].patern' (did you mean 'patterns'?)",
        ),
        (
            "detector:\n  cache: {max_sise: 5}\n",
            "'detector.cache.max_sise' (did you mean 'max_size'?)",
        ),
        ("vault:\n  type: sqlite\n  path: x.db\n", "unknown setting 'vault.path'"),
    ],
    ids=["terms", "detector", "list-item", "cache", "vault"],
)
def test_an_unknown_setting_fails_to_load(tmp_path: Path, text: str, message: str) -> None:
    # Each used to load with the key dropped, so the first masked no PERSON
    # while `privyx doctor` called it valid.
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)) as caught:
        load_config(cfg)
    assert "Zed" not in str(caught.value)  # key paths only, never values


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_home_config_is_read_without_being_named(home: Path) -> None:
    config = _write(home / ".privyx" / "config.yaml", "session:\n  strategy: client\n")

    assert active_config_paths() == [config]
    assert load_config().session.strategy == "client"


def test_a_local_config_is_ignored_until_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _write(Path.cwd() / "privyx.yaml", "vault:\n  ttl: 3600\n")

    assert load_config().vault.ttl is None
    assert f"ignoring {config}: not trusted" in caplog.text
    assert active_config_paths() == []

    caplog.clear()
    trust(config)
    assert load_config().vault.ttl == 3600
    assert caplog.text == ""
    assert active_config_paths() == [config]


def test_a_trusted_local_config_is_ignored_again_once_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # What was trusted is the content: a file edited since could now load a
    # plugin or name another upstream.
    monkeypatch.chdir(tmp_path)
    config = _write(Path.cwd() / ".privyx" / "config.yaml", "vault:\n  ttl: 3600\n")
    trust(config)

    config.write_text("upstream_url: http://127.0.0.1:9\n", encoding="utf-8")

    assert load_config().upstream_url == ""
    assert f"ignoring {config}: not trusted" in caplog.text


def test_trust_is_for_one_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "vault:\n  ttl: 3600\n"
    monkeypatch.chdir(tmp_path)
    trust(_write(Path.cwd() / "privyx.yaml", text))

    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "privyx.yaml", text)
    monkeypatch.chdir(elsewhere)

    assert load_config().vault.ttl is None


def test_local_configs_override_the_home_config_in_order(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    home_config = _write(
        home / ".privyx" / "config.yaml", "session:\n  strategy: client\nvault:\n  ttl: 100\n"
    )
    plain = _write(Path.cwd() / "privyx.yaml", "session:\n  strategy: conversation\nport: 9001\n")
    hidden = _write(Path.cwd() / ".privyx" / "config.yaml", "port: 9002\n")
    trust(plain)
    trust(hidden)

    settings = load_config()

    assert active_config_paths() == [home_config, plain, hidden]
    assert settings.vault.ttl == 100  # only the home config sets it
    assert settings.session.strategy == "conversation"  # privyx.yaml over the home config
    assert settings.port == 9002  # .privyx/config.yaml over privyx.yaml


def test_env_overrides_every_config_file(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(home / ".privyx" / "config.yaml", "vault:\n  ttl: 100\n")
    trust(_write(Path.cwd() / "privyx.yaml", "vault:\n  ttl: 200\n"))

    assert load_config().vault.ttl == 200
    monkeypatch.setenv("PRIVYX_VAULT_TTL", "300")
    assert load_config().vault.ttl == 300


@pytest.mark.parametrize("by_env", [False, True], ids=["path", "PRIVYX_CONFIG"])
def test_a_named_config_is_read_alone(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_env: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(home / ".privyx" / "config.yaml", "vault:\n  ttl: 100\n")
    trust(_write(Path.cwd() / "privyx.yaml", "port: 9001\n"))
    named = _write(tmp_path / "named" / "custom.yaml", "session:\n  strategy: client\n")
    if by_env:
        monkeypatch.setenv("PRIVYX_CONFIG", str(named))

    settings = load_config(None if by_env else named)

    assert active_config_paths(None if by_env else named) == [named]
    assert settings.session.strategy == "client"
    assert settings.vault.ttl is None
    assert settings.port == 8000


def test_no_discovery_reads_no_file_that_was_not_named(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(home / ".privyx" / "config.yaml", "vault:\n  ttl: 100\n")
    trust(_write(Path.cwd() / "privyx.yaml", "port: 9001\n"))
    monkeypatch.setenv("PRIVYX_NO_DISCOVERY", "1")

    assert active_config_paths() == []
    assert load_config().vault.ttl is None
    assert load_config().port == 8000


def test_a_mistyped_no_discovery_fails_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # Read as "no", it would leave discovery on without a word.
    monkeypatch.setenv("PRIVYX_NO_DISCOVERY", "ture")
    with pytest.raises(ConfigError, match="PRIVYX_NO_DISCOVERY"):
        load_config()


def test_in_the_home_directory_its_config_is_not_a_local_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(home)
    config = _write(home / ".privyx" / "config.yaml", "vault:\n  ttl: 100\n")

    assert find_local_configs() == []
    assert active_config_paths() == [config]
    assert load_config().vault.ttl == 100
    assert caplog.text == ""


def test_without_a_home_directory_a_project_cannot_stand_in_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `~` left as it is would name a directory of the project's own.
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.chdir(tmp_path)
    config = _write(Path.cwd() / "privyx.yaml", "port: 9001\n")
    _write(Path.cwd() / "~" / ".privyx" / "config.yaml", "port: 9002\n")
    _write(Path.cwd() / "~" / ".privyx" / "trusted.json", "{}")

    assert active_config_paths() == []
    assert load_config().port == 8000
    with pytest.raises(ConfigError, match="no home directory"):
        trust(config)
