"""Global config selection, precedence and invocation lifetime."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.main import cli


@pytest.mark.parametrize("option", ["-c", "--config"])
def test_global_config_reaches_nested_command(
    option: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        'detector:\n  patterns:\n    TICKET: "TCK-[0-9]{4}"\n', encoding="utf-8"
    )
    result = CliRunner().invoke(cli, [option, "config.yaml", "inspect", "regex", "TICKET"])
    assert result.exit_code == 0, result.output
    assert "TCK-" in result.output


@pytest.mark.parametrize("subcommand_override", [False, True])
def test_global_config_precedence(
    subcommand_override: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = [tmp_path / f"{name}.yaml" for name in ("env", "global", "command")]
    for config in configs:
        config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_CONFIG", str(configs[0]))
    args = ["-c", str(configs[1]), "config", "--path"]
    if subcommand_override:
        args += ["-c", str(configs[2])]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(configs[2 if subcommand_override else 1])
    assert os.environ["PRIVYX_CONFIG"] == str(configs[0])
    again = CliRunner().invoke(cli, ["config", "--path"])
    assert again.output.strip() == str(configs[0])


@pytest.mark.parametrize("previous", [None, "original.yaml"])
@pytest.mark.parametrize("contents", ["{}\n", "detector: [\n"])
def test_global_config_restores_environment_on_success_and_error(
    previous: str | None, contents: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if previous is None:
        monkeypatch.delenv("PRIVYX_CONFIG", raising=False)
    else:
        monkeypatch.setenv("PRIVYX_CONFIG", previous)
    config = tmp_path / "config.yaml"
    config.write_text(contents, encoding="utf-8")
    result = CliRunner().invoke(cli, ["-c", str(config), "config", "--path"])
    assert result.exit_code == (0 if contents == "{}\n" else 1)
    assert os.environ.get("PRIVYX_CONFIG") == previous


def test_global_config_is_absolute_in_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    seen = []

    def spawn_child() -> None:
        seen.append(
            subprocess.check_output(
                [sys.executable, "-c", "import os; print(os.environ['PRIVYX_CONFIG'])"],
                cwd=child_dir,
                text=True,
            ).strip()
        )

    monkeypatch.setattr("privyx.cli.main._adopt_earlier_files", spawn_child)
    result = CliRunner().invoke(cli, ["-c", "config.yaml", "config", "--path"])
    assert result.exit_code == 0, result.output
    assert seen == [str(config)]


def test_global_config_reaches_hook_without_config_option(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('token:\n  format: "[[{type}:{id}]]"\n', encoding="utf-8")
    message = tmp_path / "message.txt"
    message.write_text("fix: remove [[EMAIL:1]]\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["-c", str(config), "hook", "commit-msg", str(message)])
    assert result.exit_code == 1, result.output
    assert "placeholder [[EMAIL:1]]" in result.output


@pytest.mark.parametrize("shared_include", [False, True])
def test_global_config_preserves_symlink_relative_includes(
    shared_include: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    project.mkdir()
    shared.mkdir()
    (shared / "config.yaml").write_text("include: extra.yaml\n", encoding="utf-8")
    (project / "config.yaml").symlink_to(shared / "config.yaml")
    (project / "extra.yaml").write_text("port: 8123\n", encoding="utf-8")
    if shared_include:
        (shared / "extra.yaml").write_text("port: 8124\n", encoding="utf-8")

    runner = CliRunner()
    subcommand = runner.invoke(cli, ["config", "-c", "project/config.yaml", "--show"])
    global_option = runner.invoke(cli, ["-c", "project/config.yaml", "config", "--show"])

    assert subcommand.exit_code == 0, subcommand.output
    assert global_option.exit_code == 0, global_option.output
    assert global_option.output == subcommand.output
