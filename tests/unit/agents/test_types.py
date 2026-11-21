"""A profile's agent ``type``: chosen, guessed, given options, and added by a plugin."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from privyx import load_config
from privyx.app import Privyx
from privyx.app._serve import metadata
from privyx.cli.main import cli
from privyx.core import ConfigError


def _config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "privyx.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_adapter_is_read_as_the_type_with_a_warning(tmp_path: Path) -> None:
    config = _config(tmp_path, {"agents": {"work": {"command": "wrap", "adapter": "codex"}}})
    warnings: list[str] = []

    settings = load_config(config, warnings=warnings)

    assert settings.agents["work"].type == "codex"
    assert "adapter" not in (settings.agents["work"].model_extra or {})
    assert warnings == [
        "'agents.work.adapter' is deprecated; write 'type: codex' (removed in 0.4.0)"
    ]


def test_adapter_auto_leaves_the_type_to_the_command(tmp_path: Path) -> None:
    config = _config(tmp_path, {"agents": {"mine": {"command": "/opt/codex", "adapter": "auto"}}})
    warnings: list[str] = []

    assert load_config(config, warnings=warnings).agents["mine"].type == "codex"
    assert warnings == ["'agents.mine.adapter' is deprecated; leave out 'type' (removed in 0.4.0)"]


def test_a_profile_takes_the_options_of_its_type_only(tmp_path: Path) -> None:
    config = _config(
        tmp_path, {"agents": {"work": {"command": "claude", "endpoint_env": ["ACME_URL"]}}}
    )
    with pytest.raises(ConfigError, match="'agents.work.endpoint_env' for agent type 'claude'"):
        load_config(config)
    config = _config(tmp_path, {"agents": {"work": {"command": "x", "env_var": ["ACME_URL"]}}})
    with pytest.raises(ConfigError, match=r"'agents.work.env_var' \(did you mean 'env_vars'\?\)"):
        load_config(config)


def test_a_wrapper_of_another_type_leaves_its_parents_options(tmp_path: Path) -> None:
    config = _config(tmp_path, {"agents": {"mine": {"extends": "aider", "type": "claude"}}})

    profile = load_config(config).agents["mine"]

    assert profile.type == "claude"
    assert profile.model_extra == {}


GOOSE = """
from privyx.agents import Agent


class GooseAgent(Agent):
    name = "goose"
    metadata_keys = frozenset({"goose_machine"})

    class Options(Agent.Options):
        recipe: str = ""

    def argv(self, base_url, argv, endpoint):
        return ["--recipe", self.options.recipe, *argv]


def setup(app):
    app.add(GooseAgent)
    app.defaults(agents={"goose": {
        "command": "goose", "type": "goose", "provider": "openai",
        "env_vars": ["OPENAI_HOST"], "recipe": "review",
    }})
"""


def test_a_plugin_brings_its_agent_type_and_profile(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "goose.py").write_text(GOOSE, encoding="utf-8")
    config = _config(tmp_path, {"plugins": {"paths": [str(tmp_path / "plugins")]}})

    result = CliRunner().invoke(cli, ["-c", str(config), "run", "--dry-run", "goose", "--", "x"])

    assert result.exit_code == 0, result.output
    assert "Type: goose" in result.output
    assert "Command: goose --recipe review x" in result.output
    assert "OPENAI_HOST=http://127.0.0.1:<auto>" in result.output
    app = Privyx(config)
    assert app.settings.plugins.paths  # reading them set the plugins up
    assert "goose_machine" in metadata(app)[0]
    assert {"safeguards", "client_metadata"} <= metadata(app)[0]


def test_an_unknown_type_fails_when_its_profile_runs(tmp_path: Path) -> None:
    config = _config(tmp_path, {"agents": {"mine": {"command": "mine", "type": "nope"}}})
    assert load_config(config).agents["mine"].type == "nope"  # another config may load it

    result = CliRunner().invoke(cli, ["-c", str(config), "run", "--dry-run", "mine"])

    assert result.exit_code == 1
    assert "unknown agent type 'nope'" in result.output


def test_a_types_own_target_is_expanded(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["run", "--dry-run", "ollama:llama3.2"])
    assert result.exit_code == 0, result.output
    assert "Command: ollama run llama3.2" in result.output
    assert "Type: ollama" in result.output
    result = CliRunner().invoke(cli, ["run", "--dry-run", "ollama:"])
    assert result.exit_code == 2
    assert "name a model after ollama:" in result.output
