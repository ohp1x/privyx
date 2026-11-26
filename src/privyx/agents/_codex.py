"""The ``codex`` type: Codex, and the model provider it will call, found so it can be routed."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from privyx.agents._base import Agent, Endpoint, LaunchContext
from privyx.core import ConfigError

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


def _config(environment: Mapping[str, str], argv: list[str]) -> dict[str, Any]:
    home = Path(environment["HOME"]) if environment.get("HOME") else Path.home()
    root = Path(environment.get("CODEX_HOME") or home / ".codex")
    overrides, profile = _options(argv)
    layers = [_read(Path("/etc/codex/config.toml")), _read(root / "config.toml")]
    if profile:
        layers.append(_read(root / f"{Path(profile).name}.config.toml"))
    config: dict[str, Any] = {}
    for layer in [*layers, *map(_override, overrides)]:
        _merge(config, layer)
    return config


def openai_endpoint(environment: Mapping[str, str], argv: list[str]) -> tuple[str, str] | None:
    """Read the built-in OpenAI provider's base URL."""
    config = _config(environment, argv)
    if config.get("model_provider", "openai") != "openai":
        return None
    value = config.get("openai_base_url")
    if isinstance(value, str) and value:
        return value, "Codex config (openai_base_url)"
    return None


def custom_provider(environment: Mapping[str, str], argv: list[str]) -> tuple[str, str] | None:
    """Return the ID and base URL of the custom provider Codex will call, if any.

    Reads the system and user config, a ``-p`` profile and ``-c`` overrides. A
    provider set only in a project's ``.codex/config.toml`` is not seen.

    Raises:
        ConfigError: If a config file is unreadable, or the provider ID cannot
            be addressed by a ``-c`` override.
    """
    config = _config(environment, argv)
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


def codex_args(base_url: str, argv: list[str], provider: str | None = None) -> list[str]:
    """``argv`` with ``-c`` overrides pointing Codex, and its ``provider``, at ``base_url``."""
    override = ["-c", f"openai_base_url={json.dumps(base_url)}"]
    if provider:
        # Only the URL changes: the provider keeps its own headers and auth.
        override += ["-c", f"model_providers.{provider}.base_url={json.dumps(base_url)}"]
    # Codex drops -c before a subcommand once another -c follows it.
    end = argv.index("--") if "--" in argv else len(argv)
    own = any(arg.startswith(("-c", "--config")) for arg in argv[:end])
    return [*override, *argv[:end], *(override if own else []), *argv[end:]]


def conversation_id(headers: Mapping[str, str], body: Mapping[str, Any]) -> str | None:
    """The session of a Codex request: its ``session-id`` header, else ``thread-id``.

    A subagent sends a ``thread-id`` of its own and the ``session-id`` of the
    thread that started it, so the session holds its subagents' requests.
    ``resume`` keeps it and ``fork`` starts another (Codex 0.172.0).
    """
    return headers.get("session-id") or headers.get("thread-id") or None


class CodexAgent(Agent):
    """Codex: ``-c`` overrides outrank its config, which may name a provider of its own.

    It describes the machine it runs on in ``client_metadata`` (workspace
    paths and remote URLs), and repeats its turn metadata in a header.
    """

    name = "codex"
    metadata_keys = frozenset({"client_metadata"})
    masked_headers = frozenset({"x-codex-turn-metadata"})

    def endpoint(self, launch: LaunchContext) -> Endpoint | None:
        argv = list(launch.argv)
        custom = custom_provider(launch.environment, argv)
        if custom is not None:
            name, url = custom
            return Endpoint(url, f"Codex config (model_providers.{name})", provider=name)
        found = openai_endpoint(launch.environment, argv)
        return Endpoint(*found) if found is not None else None

    def argv(self, base_url: str, argv: list[str], endpoint: Endpoint | None) -> list[str]:
        return codex_args(base_url, argv, endpoint.provider if endpoint is not None else None)
