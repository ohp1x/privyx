"""Presets: named Privyx settings applied to a profile or to one launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.cli.main import cli
from privyx.core import ConfigError

PRESETS = {
    "verbose": {"log": {"level": "debug"}, "operator": {"type": "hash"}},
    "quiet": {"log": {"level": "error"}, "operator": {"type": "redact"}},
    "strict": {"proxy": {"passthrough_unknown": False}},
}


def _config(tmp_path: Path, data: dict[str, Any]) -> Path:
    config = tmp_path / "named.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config


def test_unused_presets_change_nothing(tmp_path: Path) -> None:
    agents = {"claude:work": {"upstream": "https://router.example.com"}}
    plain = _config(tmp_path, {"agents": agents})
    plain_sources: dict[str, str] = {}
    before = load_config(plain, agent="claude:work", sources=plain_sources)
    with_presets = tmp_path / "presets.yaml"
    with_presets.write_text(yaml.safe_dump({"agents": agents, "presets": PRESETS}))
    sources: dict[str, str] = {}
    after = load_config(with_presets, agent="claude:work", sources=sources)

    assert after.model_dump(exclude={"presets"}) == before.model_dump(exclude={"presets"})
    assert {p: s for p, s in sources.items() if not p.startswith("presets.")} == {
        p: s if s != str(plain.resolve()) else str(with_presets.resolve())
        for p, s in plain_sources.items()
    }


def test_presets_apply_left_to_right_between_the_environment_and_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path,
        {
            "presets": PRESETS,
            "agents": {
                "claude:a": {"presets": ["verbose", "quiet"]},
                "claude:b": {"presets": ["quiet", "verbose"]},
                "claude:own": {"presets": ["verbose"], "privyx": {"log": {"level": "warning"}}},
            },
        },
    )
    monkeypatch.setenv("PRIVYX_LOG_LEVEL", "critical")
    sources: dict[str, str] = {}

    a = load_config(config, agent="claude:a", sources=sources)
    b = load_config(config, agent="claude:b")
    own = load_config(config, agent="claude:own")
    chosen = load_config(config, agent="claude:own", presets=["quiet"])
    flag = load_config(
        config, agent="claude:own", presets=["quiet"], extra={"log": {"level": "info"}}
    )
    plain = load_config(config, agent="claude")

    assert (a.log.level, a.operator.type) == ("error", "redact")  # over the environment
    assert sources["operator.type"] == "presets.quiet"
    assert (b.log.level, b.operator.type) == ("debug", "hash")
    assert own.log.level == "warning"  # the profile's own settings beat its presets
    assert chosen.log.level == "error"  # a preset chosen at launch beats them
    assert flag.log.level == "info"  # and a flag beats everything
    assert (plain.log.level, plain.operator.type) == ("critical", "pseudonym")


def test_cli_presets_apply_without_a_profile(tmp_path: Path) -> None:
    config = _config(tmp_path, {"presets": PRESETS})
    settings = load_config(config, agent="my-tool", presets=["strict", "verbose"])
    assert settings.proxy.passthrough_unknown is False
    assert settings.log.level == "debug"


def test_a_child_profile_replaces_or_empties_its_parents_presets(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "presets": PRESETS,
            "agents": {
                "claude:work": {"presets": ["strict", "verbose"]},
                "claude:other": {"extends": "claude:work", "presets": ["quiet"]},
                "claude:bare": {"extends": "claude:work", "presets": []},
                "claude:same": {"extends": "claude:work"},
            },
        },
    )

    assert load_config(config, agent="claude:other").log.level == "error"
    assert load_config(config, agent="claude:other").proxy.passthrough_unknown is True
    assert load_config(config, agent="claude:bare").log.level == "info"  # the default
    same = load_config(config, agent="claude:same")
    assert (same.log.level, same.proxy.passthrough_unknown) == ("debug", False)


def test_presets_of_the_same_name_merge_across_files(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({"presets": {"debug": {"log": {"level": "debug"}}}}))
    config = _config(
        tmp_path,
        {"include": ["base.yaml"], "presets": {"debug": {"log": {"file": "/tmp/x.log"}}}},
    )
    sources: dict[str, str] = {}
    settings = load_config(config, agent="claude", presets=["debug"], sources=sources)
    assert (settings.log.level, settings.log.file) == ("debug", "/tmp/x.log")
    assert sources["presets.debug.log.level"] == str(base.resolve())


@pytest.mark.parametrize(
    ("data", "presets", "message"),
    [
        ({"agents": {"x": {"command": "x", "presets": ["strickt"]}}}, [], "'agents.x.presets'"),
        ({}, ["strickt"], "unknown preset 'strickt' (did you mean 'strict'?)"),
    ],
)
def test_an_unknown_preset_is_an_error(
    tmp_path: Path, data: dict[str, Any], presets: list[str], message: str
) -> None:
    config = _config(tmp_path, {"presets": PRESETS, **data})
    # A profile's reference is checked whichever profile runs.
    with pytest.raises(ConfigError, match="unknown preset") as error:
        load_config(config, agent="claude", presets=presets)
    assert message in str(error.value)


SECRET = "sk-" + "ant-api03-presetsecretvalue"


@pytest.mark.parametrize(
    ("preset", "message"),
    [
        ({"provider": {"api_key": SECRET}}, "'presets.x.provider'"),
        ({"upstream_url": "https://u.example.com"}, "'presets.x.upstream_url'"),
        ({"agents": {"claude": {"env": {"T": SECRET}}}}, "'presets.x.agents'"),
        ({"presets": {"y": {}}}, "'presets.x.presets'"),
        ({"include": ["more.yaml"]}, "'presets.x.include'"),
        ({"detectorz": {"regex": {"terms": {"PERSON": [SECRET]}}}}, "'presets.x.detectorz'"),
        ({"proxy": {"passthru": SECRET}}, "'presets.x.proxy.passthru'"),
    ],
)
def test_a_preset_takes_only_privyx_settings(
    tmp_path: Path, preset: dict[str, Any], message: str
) -> None:
    config = _config(tmp_path, {"presets": {"x": preset}})
    # Checked even when unused, so a typo shows before it is needed.
    with pytest.raises(ConfigError) as error:
        load_config(config)
    assert message in str(error.value)
    assert SECRET not in str(error.value)


def test_a_profile_cannot_define_presets_in_its_privyx_settings(tmp_path: Path) -> None:
    config = _config(tmp_path, {"agents": {"x": {"command": "x", "privyx": {"presets": {}}}}})
    with pytest.raises(ConfigError, match="'agents.x.privyx.presets'"):
        load_config(config)


def test_an_unused_preset_resolves_no_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIVYX_TEST_MISSING", raising=False)
    config = _config(
        tmp_path,
        {"presets": {"debug": {"log": {"level": "debug", "file": "${PRIVYX_TEST_MISSING}"}}}},
    )

    settings = load_config(config, agent="claude")
    assert settings.presets["debug"]["log"]["file"] == "${PRIVYX_TEST_MISSING}"
    with pytest.raises(ConfigError, match="PRIVYX_TEST_MISSING is not set"):
        load_config(config, agent="claude", presets=["debug"])
    monkeypatch.setenv("PRIVYX_TEST_MISSING", "/tmp/privyx-debug.log")
    assert load_config(config, presets=["debug"]).log.file == "/tmp/privyx-debug.log"


def test_dry_run_shows_the_presets_and_what_they_change(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "presets": {
                **PRESETS,
                "names": {
                    "detectors": {
                        "names": {
                            "type": "regex",
                            "builtins": False,
                            "terms": {"PERSON": ["john smith"]},
                        }
                    }
                },
                "anchored": {"anchor": {"secret": SECRET}},
            },
            "agents": {"claude:work": {"presets": ["strict", "verbose"]}},
        },
    )
    args = ["-c", str(config), "--preset", "names", "--preset", "anchored", "run"]

    result = CliRunner().invoke(cli, [*args, "--dry-run", "claude:work"])
    plain = CliRunner().invoke(cli, ["-c", str(config), "run", "--dry-run", "claude"])

    assert result.exit_code == 0, result.output
    origin = str(config.resolve())
    assert f"Presets: strict [{origin}], verbose [{origin}], names [CLI], anchored [CLI]" in (
        result.output
    )
    assert "  proxy.passthrough_unknown = false [presets.strict]" in result.output
    assert '  log.level = "debug" [presets.verbose]' in result.output
    # A mapping shows the settings in it, not the whole mapping.
    assert '  detectors.names.terms.PERSON = ["***"] [presets.names]' in result.output
    assert '  detectors.names.type = "regex" [presets.names]' in result.output
    assert "  detectors = " not in result.output
    assert '  anchor.secret = "***" [presets.anchored]' in result.output
    assert "john smith" not in result.output
    assert SECRET not in result.output
    assert plain.exit_code == 0, plain.output
    assert "Preset" not in plain.output


def _options(command: Any, path: str = "privyx") -> list[tuple[str, set[str]]]:
    found = [(path, {name for p in command.params for name in getattr(p, "opts", [])})]
    for name, sub in getattr(command, "commands", {}).items():
        found += _options(sub, f"{path} {name}")
    return found


def test_config_and_presets_are_global_options(tmp_path: Path) -> None:
    import click

    names = {name for p in cli.params for name in getattr(p, "opts", [])}
    assert {"-c", "--config", "--preset"} <= names
    # What each command took before is hidden, and read with a warning.
    detect = cli.get_command(click.Context(cli), "detect")
    assert detect is not None
    hidden = {p.name: p.hidden for p in detect.params if p.name in ("config", "preset")}
    assert hidden == {"config": True, "preset": True}
    config = _config(tmp_path, {"presets": {"redacting": {"operator": {"type": "redact"}}}})

    result = CliRunner().invoke(
        cli, ["detect", "-c", str(config), "--preset", "redacting", "mail ana@example.com"]
    )

    assert result.exit_code == 0, result.output
    assert "EMAIL" in result.stdout
    assert "`-c` after `cli detect` is deprecated; use `privyx -c FILE ...`" in result.stderr
    assert "`--preset` after `cli detect` is deprecated" in result.stderr


def test_a_preset_applies_to_any_command_and_ends_with_it(tmp_path: Path) -> None:
    from privyx.config import CHOSEN_PRESETS

    config = _config(tmp_path, {"presets": {"redacting": {"operator": {"type": "redact"}}}})
    text = "mail ana@example.com"

    masked = CliRunner().invoke(cli, ["-c", str(config), "--preset", "redacting", "mask", text])
    plain = CliRunner().invoke(cli, ["-c", str(config), "mask", text])
    shown = CliRunner().invoke(
        cli, ["-c", str(config), "--preset", "redacting", "config", "show", "--sources"]
    )
    unknown = CliRunner().invoke(cli, ["-c", str(config), "--preset", "redactin", "mask", text])

    assert masked.exit_code == 0, masked.output
    assert "[REDACTED]" in masked.output
    assert "[REDACTED]" not in plain.output
    assert "<PRIVYX_EMAIL_" in plain.output
    assert '"source": "presets.redacting"' in shown.output
    assert unknown.exit_code != 0
    assert "unknown preset 'redactin' (did you mean 'redacting'?)" in unknown.output
    # Chosen for that command only, not for what runs after it in this process.
    assert CHOSEN_PRESETS.get() == ()
    assert load_config(config).operator.type == "pseudonym"
