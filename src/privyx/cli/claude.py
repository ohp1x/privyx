"""Inspect Claude's endpoint settings before injecting the proxy URL."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from privyx.core.errors import ConfigError

BASE_URL = "ANTHROPIC_BASE_URL"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(
            "cannot inspect Claude settings file; check its JSON and permissions"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError("Claude settings must be a JSON object")
    return data


def _managed_root() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if sys.platform == "win32":
        return Path("C:/Program Files/ClaudeCode")
    return Path("/etc/claude-code")


def endpoint(environment: Mapping[str, str], argv: list[str]) -> tuple[str, str] | None:
    """Return the effective endpoint, refusing settings that defeat injection.

    Only local JSON settings can be inspected; remote policy and OS-managed
    policy need network-level enforcement outside this launcher.
    """
    options = iter(argv[: argv.index("--")] if "--" in argv else argv)
    supplied: list[str] = []
    scopes = {"user", "project", "local"}
    for arg in options:
        key, sep, value = arg.partition("=")
        if key in ("--settings", "--setting-sources"):
            value = value if sep else next(options, "")
            if key == "--settings":
                supplied.append(value)
            else:
                scopes = set(value.split(","))
    home = Path(environment.get("HOME") or Path.home())
    user = Path(environment.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    project = Path.cwd()
    # Claude scopes project settings to cwd, but local settings to the git root.
    local = project
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            env=dict(environment),
            timeout=5,
        )
        if root.returncode == 0:
            local = Path(root.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    layers = [
        ("user", user / "settings.json"),
        ("project", project / ".claude/settings.json"),
        ("local", local / ".claude/settings.local.json"),
    ]
    found = None
    for scope, path in layers:
        if scope not in scopes:
            continue
        env = _read(path).get("env", {})
        if isinstance(env, dict) and BASE_URL in env:
            found = (str(env[BASE_URL]), f"Claude {scope} settings")
    for value in supplied:
        if value.lstrip().startswith("{"):
            try:
                data = json.loads(value)
            except ValueError as exc:
                raise ConfigError("cannot inspect Claude --settings JSON") from exc
        else:
            data = _read(Path(value))
        env = data.get("env", {})
        if isinstance(env, dict) and BASE_URL in env:
            found = (str(env[BASE_URL]), "Claude --settings")
    managed = _managed_root()
    for path in [
        managed / "managed-settings.json",
        *sorted((managed / "managed-settings.d").glob("*.json")),
    ]:
        env = _read(path).get("env", {})
        if isinstance(env, dict) and BASE_URL in env:
            raise ConfigError(
                "Claude managed settings define ANTHROPIC_BASE_URL and override the proxy; "
                "ask your administrator to remove that setting and use --upstream"
            )
    if supplied and found:
        raise ConfigError(
            "Claude --settings replaces Privyx's routing settings while a settings layer "
            "defines ANTHROPIC_BASE_URL; move your --settings values to Claude settings "
            "(or CLAUDE_CONFIG_DIR) and set the destination with --upstream"
        )
    return found
