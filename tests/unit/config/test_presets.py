"""Presets: named Privyx settings applied to a profile or to one launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.config.loader import load_config
from privyx.core.errors import ConfigError

PRESETS = {
    "verbose": {"log_level": "debug", "operator": {"type": "hash"}},
    "quiet": {"log_level": "error", "operator": {"type": "redact"}},
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
                "claude:own": {"presets": ["verbose"], "privyx": {"log_level": "warning"}},
            },
        },
    )
    monkeypatch.setenv("PRIVYX_LOG_LEVEL", "critical")
    sources: dict[str, str] = {}

    a = load_config(config, agent="claude:a", sources=sources)
    b = load_config(config, agent="claude:b")
    own = load_config(config, agent="claude:own")
    chosen = load_config(config, agent="claude:own", presets=["quiet"])
    flag = load_config(config, agent="claude:own", presets=["quiet"], extra={"log_level": "info"})
    plain = load_config(config, agent="claude")

    assert (a.log_level, a.operator.type) == ("error", "redact")  # over the environment
    assert sources["operator.type"] == "presets.quiet"
    assert (b.log_level, b.operator.type) == ("debug", "hash")
    assert own.log_level == "warning"  # the profile's own settings beat its presets
    assert chosen.log_level == "error"  # a preset chosen at launch beats them
    assert flag.log_level == "info"  # and a flag beats everything
    assert (plain.log_level, plain.operator.type) == ("critical", "pseudonym")


def test_cli_presets_apply_without_a_profile(tmp_path: Path) -> None:
    config = _config(tmp_path, {"presets": PRESETS})
    settings = load_config(config, agent="my-tool", presets=["strict", "verbose"])
    assert settings.proxy.passthrough_unknown is False
    assert settings.log_level == "debug"


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

    assert load_config(config, agent="claude:other").log_level == "error"
    assert load_config(config, agent="claude:other").proxy.passthrough_unknown is True
    assert load_config(config, agent="claude:bare").log_level == "info"  # the default
    same = load_config(config, agent="claude:same")
    assert (same.log_level, same.proxy.passthrough_unknown) == ("debug", False)


def test_presets_of_the_same_name_merge_across_files(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({"presets": {"debug": {"log_level": "debug"}}}))
    config = _config(
        tmp_path,
        {"include": ["base.yaml"], "presets": {"debug": {"log_file": "/tmp/x.log"}}},
    )
    sources: dict[str, str] = {}
    settings = load_config(config, agent="claude", presets=["debug"], sources=sources)
    assert (settings.log_level, settings.log_file) == ("debug", "/tmp/x.log")
    assert sources["presets.debug.log_level"] == str(base.resolve())


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
        ({"detecter": {"terms": {"PERSON": [SECRET]}}}, "'presets.x.detecter'"),
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
        {"presets": {"debug": {"log_level": "debug", "log_file": "${PRIVYX_TEST_MISSING}"}}},
    )

    settings = load_config(config, agent="claude")
    assert settings.presets["debug"]["log_file"] == "${PRIVYX_TEST_MISSING}"
    with pytest.raises(ConfigError, match="PRIVYX_TEST_MISSING is not set"):
        load_config(config, agent="claude", presets=["debug"])
    monkeypatch.setenv("PRIVYX_TEST_MISSING", "/tmp/privyx-debug.log")
    assert load_config(config, presets=["debug"]).log_file == "/tmp/privyx-debug.log"


def test_dry_run_shows_the_presets_and_what_they_change(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "presets": {
                **PRESETS,
                "names": {
                    "detector": [
                        {"type": "regex"},
                        {"type": "yaml", "terms": {"PERSON": ["john smith"]}},
                    ]
                },
                "anchored": {"anchor": {"secret": SECRET}},
            },
            "agents": {"claude:work": {"presets": ["strict", "verbose"]}},
        },
    )
    args = ["run", "-c", str(config), "--dry-run", "claude:work"]

    result = CliRunner().invoke(cli, [*args, "--preset", "names", "--preset", "anchored"])
    plain = CliRunner().invoke(cli, ["run", "-c", str(config), "--dry-run", "claude"])

    assert result.exit_code == 0, result.output
    origin = str(config.resolve())
    assert f"Presets: strict [{origin}], verbose [{origin}], names [CLI], anchored [CLI]" in (
        result.output
    )
    assert "  proxy.passthrough_unknown = false [presets.strict]" in result.output
    assert '  log_level = "debug" [presets.verbose]' in result.output
    # A list shows the settings of its items, not the whole list.
    assert '  detector.1.terms.PERSON = ["***"] [presets.names]' in result.output
    assert '  detector.1.type = "yaml" [presets.names]' in result.output
    assert "  detector = " not in result.output
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


def test_every_command_that_reads_a_config_file_takes_a_preset() -> None:
    missing = [
        path for path, opts in _options(cli)[1:] if "--config" in opts and "--preset" not in opts
    ]
    assert missing == []


def test_a_preset_applies_to_any_command_and_ends_with_it(tmp_path: Path) -> None:
    from privyx.config.loader import CHOSEN_PRESETS

    config = _config(tmp_path, {"presets": {"redacting": {"operator": {"type": "redact"}}}})
    text = "mail ana@example.com"

    masked = CliRunner().invoke(cli, ["mask", "-c", str(config), "--preset", "redacting", text])
    plain = CliRunner().invoke(cli, ["mask", "-c", str(config), text])
    shown = CliRunner().invoke(
        cli, ["config", "--sources", "-c", str(config), "--preset", "redacting"]
    )
    unknown = CliRunner().invoke(cli, ["mask", "-c", str(config), "--preset", "redactin", text])

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
