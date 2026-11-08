"""Find the custom model provider Codex will call, so ``privyx run`` can route it."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from privyx.core.errors import ConfigError

# Codex keeps its own definition of these even when the config redefines them.
_BUILTIN = frozenset({"openai", "ollama", "lmstudio"})


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read Codex config {path}: {exc}") from exc


def _merge(base: dict[str, Any], layer: Mapping[str, Any]) -> None:
    for key, value in layer.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _override(text: str) -> dict[str, Any]:
    """Turn ``-c a.b=value`` into ``{"a": {"b": value}}``, as Codex reads it."""
    key, _, raw = text.partition("=")
    try:
        value: Any = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()  # Codex uses a value that is not TOML as a string.
    *parents, leaf = key.strip().split(".")
    layer: dict[str, Any] = {}
    node = layer
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return layer


def _options(argv: list[str]) -> tuple[list[str], str | None]:
    """Collect ``-c`` overrides and the last ``-p`` profile before any ``--``."""
    overrides: list[str] = []
    profile = None
    args = iter(argv[: argv.index("--")] if "--" in argv else argv)
    for arg in args:
        name, sep, value = arg.partition("=")
        if name in ("-c", "--config", "-p", "--profile"):
            value = value if sep else next(args, "")
        elif arg[:2] in ("-c", "-p") and len(arg) > 2:
            name, value = arg[:2], arg[2:]
        else:
            continue
        if name in ("-c", "--config"):
            overrides.append(value)
        else:
            profile = value
    return overrides, profile


def custom_provider(environment: Mapping[str, str], argv: list[str]) -> tuple[str, str] | None:
    """Return the ID and base URL of the custom provider Codex will call, if any.

    Reads the system and user config, a ``-p`` profile and ``-c`` overrides. A
    provider set only in a project's ``.codex/config.toml`` is not seen.

    Raises:
        ConfigError: If a config file is unreadable, or the provider ID cannot
            be addressed by a ``-c`` override.
    """
    root = Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
    overrides, profile = _options(argv)
    layers = [_read(Path("/etc/codex/config.toml")), _read(root / "config.toml")]
    if profile:
        layers.append(_read(root / f"{Path(profile).name}.config.toml"))
    config: dict[str, Any] = {}
    for layer in [*layers, *map(_override, overrides)]:
        _merge(config, layer)

    name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(name, str) or name in _BUILTIN or not isinstance(providers, dict):
        return None
    entry = providers.get(name)
    base_url = entry.get("base_url") if isinstance(entry, dict) else None
    if not isinstance(base_url, str):
        return None
    # Codex splits override keys on dots and does not unquote them.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ConfigError(
            f"cannot route Codex provider {name!r} through Privyx; "
            "use a provider ID of letters, digits, underscores or hyphens"
        )
    return name, base_url
