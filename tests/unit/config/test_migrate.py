"""The settings renamed in 0.3.0: read the old way with a warning, and rewritten by migrate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.app import Privyx, read_config
from privyx.cli.main import cli
from privyx.config import migrate
from privyx.core import ConfigError
from privyx.operators import Operator

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


def _operator(settings: object) -> tuple[str, object]:
    options = settings.operator.model_dump()  # type: ignore[attr-defined]
    return options["type"], options.get("id_length", options.get("text"))


def _layered(tmp_path: Path, base: dict[str, object], over: dict[str, object], via: str) -> object:
    """``over`` applied on ``base`` by a profile, a preset, or the file that includes it."""
    path = tmp_path / "c.yaml"
    options: dict[str, object] = {}
    if via == "profile":
        profile = {"command": "acme", "env_vars": ["ACME_URL"], "privyx": over}
        data = {**base, "agents": {"work": profile}}
        options["agent"] = "work"
    elif via == "preset":
        data = {**base, "presets": {"over": over}}
        options["presets"] = ["over"]
    else:
        (tmp_path / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
        data = {"include": "base.yaml", **over}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return read_config(path, **options)


@pytest.mark.parametrize("via", ["profile", "preset", "include"])
@pytest.mark.parametrize(
    ("base", "over", "expected"),
    [
        (
            {"operator": {"type": "hash", "length": 20}},
            {"operator": {"type": "pseudonym"}},
            ("pseudonym", 16),
        ),
        (
            {"operator": {"type": "redact"}, "token": {"id_length": 20}},
            {"operator": {"type": "pseudonym"}},
            ("pseudonym", 20),
        ),
        (
            {"operator": {"type": "redact", "token": "[GONE]"}},
            {"operator": {"type": "hash"}},
            ("hash", 12),
        ),
        (
            {"operator": {"type": "pseudonym", "length": 20}},
            {"operator": {"type": "hash"}},
            ("hash", 20),
        ),
        ({"token": {"id_length": 20}}, {"operator": {"type": "hash"}}, ("hash", 12)),
        (
            {"operator": {"type": "hash", "length": 20}, "token": {"id_length": 24}},
            {"operator": {"type": "encrypt"}},
            ("encrypt", 24),
        ),
        (
            {"operator": {"type": "pseudonym", "length": 20}},
            {"operator": {"length": 30}},
            ("pseudonym", 16),
        ),
        (
            {"operator": {"type": "hash", "length": 20}},
            {"operator": {"type": "redact", "token": "[GONE]"}},
            ("redact", "[GONE]"),
        ),
    ],
)
def test_an_old_operator_key_applies_to_the_type_in_use(
    tmp_path: Path,
    via: str,
    base: dict[str, object],
    over: dict[str, object],
    expected: tuple[str, object],
) -> None:
    loaded = _layered(tmp_path, base, over, via)

    assert _operator(loaded.settings) == expected  # type: ignore[attr-defined]
    assert any("is deprecated" in w for w in loaded.warnings)  # type: ignore[attr-defined]


def test_the_old_token_id_length_does_not_size_a_hash(tmp_path: Path) -> None:
    settings, warnings = _load(
        tmp_path, "operator:\n  type: hash\n  length: 20\ntoken:\n  id_length: 24\n"
    )

    assert _operator(settings) == ("hash", 20)
    assert "'token.id_length' is deprecated, and has no effect on operator type 'hash'" in (
        " ".join(warnings)
    )


def test_a_plugin_type_keeps_its_own_old_named_key_only(tmp_path: Path) -> None:
    loaded = _layered(
        tmp_path,
        {"operator": {"type": "hash", "length": 20}},
        {"operator": {"type": "acme", "token": "own"}},
        "profile",
    )

    options = loaded.settings.operator.model_dump()  # type: ignore[attr-defined]
    assert options["token"] == "own"
    assert "length" not in options


_PLUGIN = """\
from privyx.core import TransformResult
from privyx.operators import Operator


class _Plain(Operator):
    uses_anchor = False
    fixed_text = "[X]"

    async def pseudonymize(self, text, detection, session, context):
        return TransformResult(text=text, transformations=[])

    async def deanonymize(self, text, session, context):
        return TransformResult(text=text, transformations=[])


class Acme(_Plain):
    name = "acme"

    class Options(Operator.Options):
        token: str = "acme-default"


class Other(_Plain):
    name = "other"


class Third(_Plain):
    name = "third"

    class Options(Operator.Options):
        token: str = "third-default"


def setup(app):
    app.add(Acme)
    app.add(Other)
    app.add(Third)
"""


@pytest.mark.parametrize(("above", "expected"), [("other", None), ("third", "third-default")])
def test_a_plugins_option_does_not_reach_the_plugin_that_replaces_it(
    tmp_path: Path, above: str, expected: str | None
) -> None:
    plugin = tmp_path / "plug.py"
    plugin.write_text(_PLUGIN, encoding="utf-8")
    (tmp_path / "base.yaml").write_text(
        f"plugins: {{paths: ['{plugin}']}}\noperator: {{type: acme, token: own}}\n",
        encoding="utf-8",
    )
    main = tmp_path / "main.yaml"
    main.write_text(f"include: base.yaml\noperator: {{type: {above}}}\n", encoding="utf-8")

    app = Privyx(main)
    options = app.settings.operator.model_dump()
    options.pop("type")

    built = app.registry(Operator).build(above, options)

    assert getattr(built.options, "token", None) == expected


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        ("{type: hash, length: 20}", "{type: pseudonym}"),
        ("{type: redact, token: '[GONE]'}", "{type: hash}"),
    ],
)
def test_an_inherited_old_key_refuses_only_the_profile_it_does_not_fit(
    tmp_path: Path, parent: str, child: str
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "agents:\n"
        f"  parent: {{command: acme, env_vars: [ACME_URL], privyx: {{operator: {parent}}}}}\n"
        f"  child: {{extends: parent, privyx: {{operator: {child}}}}}\n",
        encoding="utf-8",
    )

    assert _operator(load_config(path))[0] == "pseudonym"
    assert _operator(load_config(path, agent="parent"))[0] == yaml.safe_load(parent)["type"]
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(path, agent="child")


def test_a_new_id_length_is_not_inherited_across_hash(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "agents:\n"
        "  parent:\n    command: acme\n    env_vars: [ACME_URL]\n"
        "    privyx: {operator: {type: hash, id_length: 20}}\n"
        "  child: {extends: parent, privyx: {operator: {type: pseudonym}}}\n",
        encoding="utf-8",
    )

    assert _operator(load_config(path, agent="parent")) == ("hash", 20)
    assert _operator(load_config(path, agent="child")) == ("pseudonym", 16)


@pytest.mark.parametrize("key", ["length", "id_length"])
def test_writing_the_new_key_as_warned_keeps_a_later_presets_id(tmp_path: Path, key: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "presets:\n"
        f"  hashed: {{operator: {{type: hash, {key}: 20}}}}\n"
        f"  encrypted: {{operator: {{type: encrypt, key: '{'ab' * 32}'}}}}\n",
        encoding="utf-8",
    )

    assert _operator(load_config(path, presets=["hashed", "encrypted"])) == ("encrypt", 16)
    assert _operator(load_config(path, presets=["hashed"])) == ("hash", 20)


@pytest.mark.parametrize(
    ("migrated", "other", "choose"),
    [
        (
            "operator: {type: hash, length: 20}\n",
            "presets:\n  own: {operator: {type: pseudonym}}\n",
            {"presets": ["own"]},
        ),
        (
            "operator: {type: pseudonym}\ntoken: {id_length: 20}\n",
            "presets:\n  own: {operator: {type: hash}}\n",
            {"presets": ["own"]},
        ),
        (
            "token: {id_length: 20}\n",
            "presets:\n  own: {operator: {type: hash}}\n",
            {"presets": ["own"]},
        ),
        (
            "operator: {type: redact}\ntoken: {id_length: 20}\n",
            "presets:\n  own: {operator: {type: pseudonym}}\n",
            {"presets": ["own"]},
        ),
        (
            "operator: {type: hash, length: 20}\n",
            "agents:\n  work:\n    command: acme\n    env_vars: [ACME_URL]\n"
            "    privyx: {operator: {type: pseudonym}}\n",
            {"agent": "work"},
        ),
    ],
)
def test_migrating_one_file_keeps_what_another_files_type_uses(
    tmp_path: Path, migrated: str, other: str, choose: dict[str, object]
) -> None:
    lower = tmp_path / "lower.yaml"
    lower.write_text(migrated, encoding="utf-8")
    main = tmp_path / "main.yaml"
    main.write_text("include: lower.yaml\n" + other, encoding="utf-8")
    before = _operator(load_config(main, **choose))

    result = CliRunner().invoke(cli, ["config", "migrate", str(lower), "--write"])

    assert result.exit_code == 0, result.output
    assert _operator(load_config(main, **choose)) == before


@pytest.mark.parametrize(
    "text",
    [
        "operator:\n  type: pseudonym\n  length: 12\n",
        "operator:\n  length: 12\n",
        "operator:\n  type: hash\n  token: x\n",
    ],
)
def test_an_old_operator_key_beside_a_type_that_does_not_use_it_is_refused(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(ConfigError, match="unknown setting"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    ("top", "patch", "key"),
    [
        ("operator: {type: hash}\n", "operator: {length: 30}", "operator.length"),
        ("operator: {type: redact}\n", "token: {id_length: 30}", "token.id_length"),
    ],
)
def test_migrate_leaves_the_key_of_a_preset_without_a_type(
    tmp_path: Path, top: str, patch: str, key: str
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(f"{top}presets:\n  own: {{{patch}}}\n", encoding="utf-8")
    before = _effective(path)

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert f"left {key} as written" in result.stderr
    assert _effective(path) == before


def test_an_old_key_a_preset_cannot_use_refuses_it_when_chosen(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "presets:\n  own:\n    operator:\n      type: pseudonym\n      length: 12\n",
        encoding="utf-8",
    )

    assert _operator(load_config(path))[0] == "pseudonym"
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(path, presets=["own"])


def _with_profile(top: str, operator: str) -> str:
    return (
        f"{top}agents:\n  work:\n    command: acme\n    env_vars: [ACME_URL]\n"
        f"    privyx:\n      operator: {operator}\n"
    )


def _with_preset(top: str, operator: str) -> str:
    return f"{top}presets:\n  own:\n    operator: {operator}\n"


def _effective(path: Path) -> list[tuple[str, object] | None]:
    """The operator for each profile and preset of the file; ``None`` where it is refused."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    options: list[dict[str, object]] = [{}]
    options += [{"agent": name} for name in data.get("agents", {})]
    options += [{"presets": [name]} for name in data.get("presets", {})]
    found: list[tuple[str, object] | None] = []
    for kw in options:
        try:
            found.append(_operator(load_config(path, **kw)))
        except ConfigError:
            found.append(None)
    return found


@pytest.mark.parametrize("patched", [_with_profile, _with_preset])
@pytest.mark.parametrize(
    ("top", "operator", "key"),
    [
        ("operator: {type: hash, length: 20}\n", "{type: pseudonym}", "operator.length"),
        ("operator: {type: hash, length: 20}\n", "{type: encrypt}", "operator.length"),
        (
            "operator: {type: redact}\ntoken: {id_length: 20}\n",
            "{type: pseudonym}",
            "token.id_length",
        ),
        (
            "operator: {type: pseudonym}\ntoken: {id_length: 20}\n",
            "{type: hash}",
            "token.id_length",
        ),
        ("operator: {type: pseudonym, length: 20}\n", "{type: hash}", "operator.length"),
        ("operator: {type: hash, token: '[GONE]'}\n", "{type: redact}", "operator.token"),
    ],
)
def test_migrate_leaves_a_key_a_profile_or_preset_would_read_differently(
    tmp_path: Path, patched, top: str, operator: str, key: str
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("port: 8001\n" + patched(top, operator), encoding="utf-8")
    before = _effective(path)

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert result.exit_code == 0, result.output
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["server"] == {"port": 8001} and "port" not in written
    section, name = key.split(".")
    assert name in written[section]
    assert f"left {key} as written" in result.stderr
    assert "where that type is picked" in result.stderr
    assert _effective(path) == before


def test_migrate_names_a_key_it_leaves_without_its_value(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        _with_profile("operator: {type: redact, token: '[GONE]'}\n", "{type: acme}"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert "left operator.token as written" in result.stderr
    assert "[GONE]" not in result.stderr


@pytest.mark.parametrize(
    "top",
    ["operator: {type: hash, length: 20}\n", "operator: {type: hash}\ntoken: {id_length: 20}\n"],
)
def test_migrate_does_not_rewrite_a_file_holding_only_keys_it_leaves(
    tmp_path: Path, top: str
) -> None:
    path = tmp_path / "c.yaml"
    text = "# mine\n" + _with_profile(top, "{type: pseudonym}")
    path.write_text(text, encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8") == text
    assert list(tmp_path.glob("*.bak*")) == []
    assert "as written" in result.stderr
    assert "Rewrote" not in result.stderr


def test_migrate_does_not_rewrite_a_file_that_only_picks_hash(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    text = "# mine\noperator: {type: hash}\n"
    path.write_text(text, encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8") == text
    assert list(tmp_path.glob("*.bak*")) == []
    assert "nothing to migrate" in result.stderr


@pytest.mark.parametrize(
    ("top", "operator", "new"),
    [
        ("operator: {type: hash, length: 20}\n", "{type: hash}", {"type": "hash", "id_length": 20}),
        (
            "operator: {type: pseudonym}\ntoken: {id_length: 20}\n",
            "{type: encrypt}",
            {"type": "pseudonym", "id_length": 20},
        ),
        (
            "operator: {type: redact, token: '[GONE]'}\n",
            "{type: hash}",
            {"type": "redact", "text": "[GONE]"},
        ),
    ],
)
def test_migrate_moves_a_key_every_type_in_the_file_reads_the_same(
    tmp_path: Path, top: str, operator: str, new: dict[str, object]
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(_with_profile(top, operator), encoding="utf-8")
    before = _effective(path)

    result = CliRunner().invoke(cli, ["config", "migrate", str(path), "--write"])

    assert result.exit_code == 0, result.output
    assert "as written" not in result.stderr
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["operator"] == new
    assert _effective(path) == before


@pytest.mark.parametrize(
    ("base", "main", "target", "written"),
    [
        (
            {"operator": {"type": "redact"}},
            {"operator": {"token": "[HIDDEN]"}},
            "main.yaml",
            {"operator": {"text": "[HIDDEN]"}},
        ),
        (
            {"operator": {"type": "hash"}},
            {"operator": {"length": 8}},
            "main.yaml",
            {"operator": {"type": "hash", "id_length": 8}},
        ),
        (
            {"operator": {"token": "[HIDDEN]"}},
            {"operator": {"type": "redact"}},
            "base.yaml",
            {"operator": {"text": "[HIDDEN]"}},
        ),
        (
            {"operator": {"type": "redact"}},
            {"token": {"id_length": 8}},
            "main.yaml",
            {"token": {}},
        ),
    ],
    ids=["token-over-type", "length-over-type", "token-under-type", "unused-id-length"],
)
def test_migrate_reads_old_keys_by_the_type_of_the_whole_chain(
    tmp_path: Path,
    base: dict[str, object],
    main: dict[str, object],
    target: str,
    written: dict[str, object],
) -> None:
    (tmp_path / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text(yaml.safe_dump({"include": ["base.yaml"], **main}), encoding="utf-8")
    file = tmp_path / target
    before = _operator(load_config(path))
    app = Privyx(config=path)

    first = app.config.migrate(file, write=True)

    assert first.renamed
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    assert {k: v for k, v in data.items() if k != "include"} == written
    assert _operator(load_config(path)) == before
    old = [w for w in read_config(path).warnings if "operator.t" in w or "id_length" in w]
    assert [w for w in old if "'hash'" not in w] == []
    assert app.config.migrate(file, write=True).renamed == []


def test_migrate_keeps_an_unused_key_that_covers_one_in_another_file(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(
        "operator: {type: pseudonym, length: 20}\n", encoding="utf-8"
    )
    path = tmp_path / "main.yaml"
    path.write_text("include: [base.yaml]\noperator: {length: 30}\n", encoding="utf-8")
    before = _operator(load_config(path))

    migrated = Privyx(config=path).config.migrate(path, write=True)

    assert migrated.left == ["operator.length"]
    assert _operator(load_config(path)) == before


def test_migrate_keeps_a_key_the_type_from_the_environment_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "base.yaml").write_text("operator: {type: redact}\n", encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text("include: [base.yaml]\ntoken: {id_length: 20}\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_OPERATOR__TYPE", "pseudonym")
    before = _operator(load_config(path))
    assert before == ("pseudonym", 20)

    migrated = Privyx(config=path).config.migrate(path, write=True)

    assert migrated.left == ["token.id_length"]
    assert _operator(load_config(path)) == before
    monkeypatch.delenv("PRIVYX_OPERATOR__TYPE")
    assert _operator(load_config(path))[0] == "redact"  # still loads without it


@pytest.mark.parametrize(
    ("lower", "upper", "key"),
    [
        ("{type: pseudonym, token: '[HIDDEN]'}", "redact", "operator.token"),
        ("{type: pseudonym, length: 20}", "hash", "operator.length"),
    ],
)
def test_migrate_names_a_key_only_the_type_above_reads(
    tmp_path: Path, lower: str, upper: str, key: str
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(f"operator: {lower}\n", encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text(f"include: [base.yaml]\noperator: {{type: {upper}}}\n", encoding="utf-8")
    written = base.read_text(encoding="utf-8")
    before = _operator(load_config(path))

    migrated = Privyx(config=path).config.migrate(base, write=True)

    assert migrated.left == [key]
    assert migrated.renamed == []
    assert base.read_text(encoding="utf-8") == written
    assert _operator(load_config(path)) == before


def test_migrate_leaves_a_length_when_a_written_type_would_refuse_its_neighbor(
    tmp_path: Path,
) -> None:
    (tmp_path / "base.yaml").write_text("operator: {type: hash}\n", encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text(
        "include: [base.yaml]\noperator: {length: 8, text: '[REDACTED]'}\n", encoding="utf-8"
    )
    before = _operator(load_config(path))
    assert before == ("hash", 8)

    migrated = Privyx(config=path).config.migrate(path, write=True)

    assert migrated.left == ["operator.length"]
    assert _operator(load_config(path)) == before


@pytest.mark.parametrize(
    ("base", "main", "target", "value", "key"),
    [
        (
            'operator: {type: "${EXAMPLE_OPERATOR}"}\n',
            "token: {id_length: 20}\n",
            "main.yaml",
            "pseudonym",
            "token.id_length",
        ),
        (
            "operator: {type: pseudonym, token: '[HIDDEN]'}\n",
            'operator: {type: "${EXAMPLE_OPERATOR}"}\n',
            "base.yaml",
            "redact",
            "operator.token",
        ),
        (
            "log: {level: info}\n",
            "operator: {type: \"${EXAMPLE_OPERATOR}\", token: '[HIDDEN]'}\n",
            "main.yaml",
            "redact",
            "operator.token",
        ),
    ],
    ids=["type-in-another-file", "type-above-reads-it", "type-in-the-file"],
)
def test_migrate_keeps_a_key_while_a_type_is_a_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: str,
    main: str,
    target: str,
    value: str,
    key: str,
) -> None:
    (tmp_path / "base.yaml").write_text(base, encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text("include: [base.yaml]\n" + main, encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_OPERATOR", value)
    file = tmp_path / target
    written = file.read_text(encoding="utf-8")
    before = _operator(load_config(path))

    migrated = Privyx(config=path).config.migrate(file, write=True)

    assert migrated.left == [key]
    assert file.read_text(encoding="utf-8") == written
    assert _operator(load_config(path)) == before


def test_migrate_keeps_a_key_a_preset_of_the_file_may_read_by_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "base.yaml").write_text("operator: {type: redact}\n", encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text(
        "include: [base.yaml]\ntoken: {id_length: 20}\n"
        'presets:\n  own: {operator: {type: "${EXAMPLE_OPERATOR}"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EXAMPLE_OPERATOR", "pseudonym")
    before = _operator(load_config(path, presets=["own"]))
    assert before == ("pseudonym", 20)

    migrated = Privyx(config=path).config.migrate(path, write=True)

    assert migrated.left == ["token.id_length"]
    assert _operator(load_config(path, presets=["own"])) == before


def test_migrate_keeps_a_key_of_a_preset_without_a_type_in_a_mixed_chain(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(
        "operator: {type: redact}\npresets:\n  own: {operator: {token: '[LOW]'}}\n",
        encoding="utf-8",
    )
    path = tmp_path / "main.yaml"
    path.write_text(
        "include: [base.yaml]\noperator: {type: hash, token: '[HIGH]'}\n", encoding="utf-8"
    )
    before = _operator(load_config(path, presets=["own"]))

    migrated = Privyx(config=path).config.migrate(base, write=True)

    assert migrated.left == ["operator.token"]
    assert _operator(load_config(path, presets=["own"])) == before


def test_migrate_leaves_a_key_when_the_chain_can_apply_several_types(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("token: {id_length: 20}\n", encoding="utf-8")
    path = tmp_path / "main.yaml"
    path.write_text(
        "include: [base.yaml]\npresets:\n  own: {operator: {type: redact}}\n", encoding="utf-8"
    )
    plain, chosen = _operator(load_config(path)), _operator(load_config(path, presets=["own"]))

    migrated = Privyx(config=path).config.migrate(tmp_path / "base.yaml", write=True)

    assert migrated.left == ["token.id_length"]
    assert _operator(load_config(path)) == plain
    assert _operator(load_config(path, presets=["own"])) == chosen
