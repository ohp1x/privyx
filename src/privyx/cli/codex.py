"""Discover Codex provider IDs so routing overrides preserve their auth settings."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from privyx.core.errors import ConfigError

_BUILTINS = {"openai", "ollama", "lmstudio"}


def _options(argv: list[str]) -> list[tuple[str, str]]:
    """Read config/profile/cwd options, stopping before literal prompt arguments."""
    result = []
    index = 0
    while index < len(argv) and argv[index] != "--":
        arg = argv[index]
        key, sep, value = arg.partition("=")
        if key in {"-c", "--config", "-p", "--profile", "-C", "--cd"}:
            if not sep and index + 1 < len(argv):
                index += 1
                value = argv[index]
            result.append((key, value))
        elif arg.startswith(("-c", "-p", "-C")):
            result.append((arg[:2], arg[2:]))
        index += 1
    return result


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read Codex config {path}: {exc}") from exc


def provider_names(environment: Mapping[str, str], argv: list[str]) -> tuple[str, ...]:
    """Snapshot provider names from config layers and CLI without copying credentials.

    Only the providers' base URLs will be overridden. Reading every definition
    also covers provider switches in legacy profiles or resumed sessions.
    """
    home = Path(environment.get("HOME") or os.path.expanduser("~"))
    root = Path(environment.get("CODEX_HOME") or home / ".codex")
    options = _options(argv)
    cwd = Path.cwd()
    for key, value in options:
        if key in {"-C", "--cd"}:
            cwd = (cwd / value).resolve()
    paths = [Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml")]
    paths += [root / "config.toml"]
    paths += [parent / ".codex/config.toml" for parent in (*reversed(cwd.parents), cwd)]
    documents = [_read(path) for path in dict.fromkeys(paths)]
    profiles: set[str] = set()
    for key, value in options:
        if key in {"-p", "--profile"}:
            profiles.add(value)
        elif key in {"-c", "--config"}:
            try:
                documents.append(tomllib.loads(value))
            except tomllib.TOMLDecodeError:
                # Codex treats a non-TOML override value as a string.
                name, sep, raw = value.partition("=")
                if sep and name.strip() in {"model_provider", "profile"}:
                    documents.append({name.strip(): raw.strip()})
    for document in documents:
        if isinstance(profile := document.get("profile"), str):
            profiles.add(profile)
    for profile in sorted(profiles):
        if re.fullmatch(r"[\w-]+", profile):
            documents.append(_read(root / f"{profile}.config.toml"))

    names: set[str] = set()
    for document in documents:
        layers = [document]
        legacy = document.get("profiles", {})
        if isinstance(legacy, dict):
            layers += [profile for profile in legacy.values() if isinstance(profile, dict)]
        for layer in layers:
            if isinstance(provider := layer.get("model_provider"), str):
                names.add(provider)
            providers = layer.get("model_providers", {})
            if isinstance(providers, dict):
                names.update(providers)
    names -= _BUILTINS
    for name in names:
        # Codex splits override keys on dots and does not unquote TOML keys.
        # Reject IDs we cannot address without replacing auth-bearing tables.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(
                f"cannot safely override URL of Codex provider {name!r}; "
                "use a provider ID containing only letters, digits, underscores or hyphens"
            )
    return tuple(sorted(names))
