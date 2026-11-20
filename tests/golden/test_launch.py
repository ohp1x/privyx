"""The launch plan of each built-in profile in each scenario, against its golden file.

A case holds what ``privyx run --dry-run`` prints, and the command line and
variables the tool would be started with.  The environment is emptied first
and the working directory is an empty one, so nothing of the machine's own
setup reaches the plan.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import privyx
import privyx.cli.commands.run as run_command
from privyx.cli.main import cli

PROFILES = ("aider", "claude", "codex", "gemini", "grok", "ollama", "openai", "opencode", "pi")

#: The variables a test process keeps; every other one is removed.
_KEPT = frozenset({"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"})

#: Each tool's own endpoint, set where the tool reads it.
ENDPOINTS = {
    "ANTHROPIC_BASE_URL": "https://anthropic.example.com",
    "OPENAI_BASE_URL": "https://openai.example.com/v1",
    "OPENAI_API_BASE": "https://aider.example.com/v1",
    "GROK_XAI_API_BASE_URL": "https://grok.example.com/v1",
    "GOOGLE_GEMINI_BASE_URL": "https://gemini.example.com",
    "CODE_ASSIST_ENDPOINT": "https://code-assist.example.com",
    "GOOGLE_VERTEX_BASE_URL": "https://vertex.example.com",
    "OLLAMA_HOST": "ollama.example.com",
}
CODEX_CONFIG = """\
model_provider = "router"

[model_providers.router]
name = "Router"
base_url = "https://codex.example.com/v1"
"""

#: Each scenario: options before the target, the target (``{p}`` is the
#: profile), arguments after it, environment, Codex config, and config file.
SCENARIOS: dict[str, dict[str, Any]] = {
    "default": {},
    "arguments": {"args": ["-c", "model=acme", "two words"]},
    "endpoint": {"env": ENDPOINTS, "codex": True},
    "upstream": {
        "env": ENDPOINTS,
        "codex": True,
        "options": ["--upstream", "https://upstream.example.com/v1"],
    },
    "provider": {"options": ["--provider", "google"]},
    "privyx-key": {"env": {**ENDPOINTS, "PRIVYX_API_KEY": "golden-privyx-key"}, "codex": True},
    "gateway": {"env": ENDPOINTS, "codex": True, "config": {"proxy": {"mode": "gateway"}}},
    "wrapper": {
        "env": ENDPOINTS,
        "codex": True,
        "target": "wrap",
        "config": {"agents": {"wrap": {"extends": "{p}", "command": "/opt/acme/bin/wrap"}}},
    },
    "variant": {
        "target": "{p}:work",
        "config": {"agents": {"{p}:work": {"env": {"ACME_TEAM": "apollo"}}}},
    },
    "model": {"target": "{p}:llama3.2"},
}


@pytest.fixture(autouse=True)
def bare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable but the basic ones, and an empty working directory."""
    for name in list(os.environ):
        if name not in _KEPT:
            monkeypatch.delenv(name)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)


def test_every_built_in_profile_is_covered() -> None:
    result = CliRunner().invoke(cli, ["run", "--list"])
    assert result.exit_code == 0, result.output
    assert tuple(sorted(line.split()[0] for line in result.output.splitlines()[1:])) == PROFILES


@pytest.mark.parametrize("profile", PROFILES)
def test_launch_plan(
    profile: str,
    golden: Callable[[str, Any], None],
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans: list[Any] = []
    build = run_command.build_launch_plan

    def capture(*args: Any, **kwargs: Any) -> Any:
        plans.append(build(*args, **kwargs))
        return plans[-1]

    monkeypatch.setattr(run_command, "build_launch_plan", capture)
    paths = {
        str(tmp_path): "<tmp>",
        str(home): "<home>",
        str(Path(privyx.__file__).parent): "<privyx>",
    }
    cases = {}
    for name, scenario in SCENARIOS.items():
        scenario = json.loads(json.dumps(scenario).replace("{p}", profile))
        plans.clear()
        cases[name] = _scrub(_case(scenario, profile, home, tmp_path, monkeypatch, plans), paths)
    golden(f"launch/{profile}", cases)


def _case(
    scenario: dict[str, Any],
    profile: str,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plans: list[Any],
) -> dict[str, Any]:
    with monkeypatch.context() as scope:
        for key, value in scenario.get("env", {}).items():
            scope.setenv(key, value)
        codex = home / ".codex" / "config.toml"
        codex.parent.mkdir(exist_ok=True)
        codex.write_text(CODEX_CONFIG if scenario.get("codex") else "")
        argv = []
        if "config" in scenario:
            config = tmp_path / "config.yaml"
            config.write_text(yaml.safe_dump(scenario["config"]))
            argv += ["-c", str(config)]
        argv += ["run", "--dry-run", *scenario.get("options", [])]
        argv.append(scenario.get("target", profile))
        if "args" in scenario:
            argv += ["--", *scenario["args"]]
        result = CliRunner().invoke(cli, argv, prog_name="privyx")
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    case: dict[str, Any] = {
        "run": shlex.join(["privyx", *argv]),
        "exit_code": result.exit_code,
        "output": result.output.splitlines(),
    }
    if plans:
        plan = plans[-1]
        env = dict(plan.injected_env)
        if "PRIVYX_PI_TOKEN" in env:
            env["PRIVYX_PI_TOKEN"] = "<new each run>"
        case["launch"] = {"argv": [plan.command, *plan.argv], "env": env}
    return case


def _scrub(value: Any, paths: dict[str, str]) -> Any:
    """``value`` with each path of ``paths`` replaced by its name, in every string."""
    if isinstance(value, str):
        for path, name in sorted(paths.items(), key=lambda item: -len(item[0])):
            value = value.replace(path, name)
        return value
    if isinstance(value, list):
        return [_scrub(item, paths) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, paths) for key, item in value.items()}
    return value
