"""The settings renamed in 0.3.0: read the old way with a warning, and rewritten by migrate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.app import read_config
from privyx.cli.main import cli
from privyx.config import migrate
from privyx.core import ConfigError

CONFIGS = Path(__file__).parents[3] / "configs"

OLD = """\
host: 0.0.0.0
port: 8001
log_level: debug
logging: json
log_file: /tmp/privyx-debug.log
tls: {certfile: cert.pem, keyfile: key.pem}
audit:
  enabled: false
  path: /tmp/audit.log
  tap: true
  tap_dir: /tmp/tap
  prices: {claude-x: {input: 3, output: 15}}
tracing: {enabled: true}
proxy:
  mode: gateway
  directive: true
  cache_control: true
detector:
  - type: regex
    terms: {PERSON: [ann]}
  - type: yaml
    patterns: {CODE: 'X\\d+'}
"""


def _load(tmp_path: Path, text: str, name: str = "c.yaml") -> tuple[object, list[str]]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    loaded = read_config(path)
    warnings = loaded.warnings
    return loaded.settings, warnings


def test_the_old_keys_are_read_as_the_new_ones_with_a_warning(tmp_path: Path) -> None:
    settings, warnings = _load(tmp_path, OLD)

    assert (settings.server.host, settings.server.port) == ("0.0.0.0", 8001)  # type: ignore[attr-defined]
    assert settings.server.tls.enabled  # type: ignore[attr-defined]
    log = settings.log  # type: ignore[attr-defined]
    assert (log.level, log.format, log.file) == ("debug", "json", "/tmp/privyx-debug.log")
    features = settings.features  # type: ignore[attr-defined]
    assert {name: entry.enabled for name, entry in features.items()} == {
        "audit": False,
        "metrics": True,
        "tracing": True,
        "directive": True,
        "cache_control": True,
    }
    assert features["audit"].model_extra["path"] == "/tmp/audit.log"
    assert settings.proxy.tap and settings.proxy.tap_dir == "/tmp/tap"  # type: ignore[attr-defined]
    detectors = {n: d.model_dump() for n, d in settings.detectors.items()}  # type: ignore[attr-defined]
    assert detectors["regex"]["terms"] == {"PERSON": ["ann"]}
    assert detectors["regex"]["builtins"] is True
    assert (detectors["regex-2"]["builtins"], detectors["regex-2"]["patterns"]) == (
        False,
        {"CODE": "X\\d+"},
    )
    assert "'port' is deprecated; write 'server.port' (removed in 0.4.0)" in warnings
    assert "'proxy.directive' is deprecated; write 'features.directive' (removed in 0.4.0)" in (
        warnings
    )
    assert (
        "'detector' is deprecated; write 'detectors.regex', 'detectors.regex-2' "
        "with 'builtins: false' for type 'yaml' (removed in 0.4.0)"
    ) in warnings
    assert not [w for w in warnings if "ann" in w or "8001" in w]  # names, never values


def test_migrate_writes_a_config_that_loads_the_same_without_a_warning(tmp_path: Path) -> None:
    old, _ = _load(tmp_path, OLD)
    renamed: list[str] = []
    data = migrate(yaml.safe_load(OLD), renamed)

    new, warnings = _load(tmp_path, yaml.safe_dump(data, sort_keys=False), "new.yaml")

    assert renamed and warnings == []
    assert new.model_dump() == old.model_dump()  # type: ignore[attr-defined]
    assert list(data) == ["server", "log", "features", "proxy", "detectors"]
    assert data["detectors"] == {
        "regex": {"terms": {"PERSON": ["ann"]}, "builtins": True},
        "regex-2": {"type": "regex", "patterns": {"CODE": "X\\d+"}, "builtins": False},
    }
    assert data["features"]["directive"] is True and data["features"]["audit"]["enabled"] is False


def _found(config: Path) -> set[str]:
    result = CliRunner().invoke(
        cli, ["-c", str(config), "detect", "--no-policy", "mail ana@example.com, ann met bob: X12"]
    )
    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.stdout.splitlines()]
    return {row[1] for row in rows if len(row) > 1 and re.fullmatch(r"\d+:\d+", row[0])}


@pytest.mark.parametrize(
    ("base", "top", "found"),
    [
        # Naming regex over yaml turns the built-in patterns back on.
        ("{type: yaml, patterns: {CODE: 'X\\d+'}}", "{type: regex}", {"CODE", "EMAIL"}),
        # A list replaces what the layers below set.
        ("{disable: [EMAIL]}", "[{type: regex}]", {"EMAIL"}),
        ("{patterns: {EMAIL: 'no-match'}}", "[{type: regex}]", {"EMAIL"}),
        ("{terms: {P: [ann]}}", "[{type: regex}]", {"EMAIL"}),
        # A mapping sets each detector that is on, with a type too.
        (
            "[{type: regex, terms: {A: [ann]}}, {type: yaml, patterns: {C: 'X\\d+'}}]",
            "{type: yaml, terms: {B: [bob]}}",
            {"A", "B", "C"},
        ),
        (
            "[{type: regex, disable: [EMAIL]}, {type: yaml, terms: {A: [ann]}}]",
            "{terms: {B: [bob]}}",
            {"A", "B"},
        ),
        ("{type: yaml, patterns: {CODE: 'X\\d+'}}", "{terms: {P: [ann]}}", {"CODE", "P"}),
    ],
)
def test_old_detector_layers_find_what_they_did_before_and_after_migrate(
    tmp_path: Path, base: str, top: str, found: set[str]
) -> None:
    (tmp_path / "base.yaml").write_text(f"detector: {base}\n", encoding="utf-8")
    config = tmp_path / "top.yaml"
    config.write_text(f"include: base.yaml\ndetector: {top}\n", encoding="utf-8")

    assert _found(config) == found

    migrated = CliRunner().invoke(cli, ["-c", str(config), "config", "migrate", "--write"])

    assert migrated.exit_code == 0, migrated.output
    assert "detector:" not in config.read_text(encoding="utf-8")
    assert "detector:" not in (tmp_path / "base.yaml").read_text(encoding="utf-8")
    assert _found(config) == found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"detector": {"terms": {"P": ["a"]}}}, {"detectors": {"regex": {"terms": {"P": ["a"]}}}}),
        ({"detector": {"type": "presidio"}}, {"detectors": {"regex": False, "presidio": {}}}),
        (
            {"detector": {"type": "yaml", "patterns": {"C": "x"}}},
            {"detectors": {"regex": {"patterns": {"C": "x"}, "builtins": False}}},
        ),
        ({"tracing": {"enabled": False}}, {"features": {"tracing": False}}),
        ({"port": 1, "server": {"port": 2}}, {"server": {"port": 2}}),  # the new key wins
        (
            {"upstream_url": "https://h.example.com"},
            {"provider": {"base_url": "https://h.example.com"}},
        ),
        (
            {
                "agents": {
                    "x": {"command": "x", "adapter": "codex", "privyx": {"log_level": "info"}}
                }
            },
            {
                "agents": {
                    "x": {"command": "x", "type": "codex", "privyx": {"log": {"level": "info"}}}
                }
            },
        ),
        (
            {"presets": {"p": {"audit": {"enabled": False}}}},
            {"presets": {"p": {"features": {"audit": False}}}},
        ),
    ],
)
def test_migrate_writes_each_old_key_the_new_way(old: dict, new: dict) -> None:
    renamed: list[str] = []
    assert migrate(old, renamed) == new
    assert renamed


def test_migrate_leaves_a_config_without_old_keys_as_it_is() -> None:
    renamed: list[str] = []
    data = {"server": {"port": 1}, "detectors": {"regex": {"terms": {}}}}
    assert migrate(data, renamed) == data
    assert renamed == []


@pytest.mark.parametrize("path", sorted(CONFIGS.rglob("*.yaml")), ids=lambda p: p.name)
def test_each_shipped_config_needs_no_migration(path: Path) -> None:
    renamed: list[str] = []
    migrate(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, renamed)
    assert renamed == []


def test_an_unknown_key_of_a_section_that_is_gone_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown setting 'audit.enabeld' \(did you mean"):
        _load(tmp_path, "audit:\n  enabeld: false\n  path: /tmp/a.log\n")


def test_an_old_key_in_a_profile_or_a_preset_is_read_too(tmp_path: Path) -> None:
    text = (
        "agents:\n  claude:quiet:\n    privyx:\n      log_level: error\n"
        "presets:\n  verbose:\n    log_level: debug\n"
    )
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    warnings: list[str] = []

    loaded = read_config(path, agent="claude:quiet")
    warnings = loaded.warnings
    quiet = loaded.settings
    verbose = load_config(path, presets=["verbose"])

    assert (quiet.log.level, verbose.log.level) == ("error", "debug")
    assert "'agents.claude:quiet.privyx.log_level' is deprecated; write " in " ".join(warnings)


def test_type_yaml_in_a_detectors_entry_is_regex_without_builtins(tmp_path: Path) -> None:
    settings, warnings = _load(tmp_path, "detectors:\n  own:\n    type: yaml\n")

    own = settings.detectors["own"]  # type: ignore[attr-defined]
    assert (own.type, own.model_dump()["builtins"]) == ("regex", False)
    assert any("detectors.own.type 'yaml' is deprecated" in w for w in warnings)
