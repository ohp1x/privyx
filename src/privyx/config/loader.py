"""Configuration loader — merges defaults, YAML file, and environment.

Precedence (low → high):
    1. Built-in defaults
    2. YAML config file (``--config`` or ``PRIVYX_CONFIG``)
    3. Environment variables
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from privyx.config.defaults import DEFAULTS
from privyx.config.env import env_config
from privyx.config.schema import Settings
from privyx.core.errors import ConfigError


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    path: str | Path | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> Settings:
    """Load and merge configuration into validated :class:`Settings`."""
    merged: dict[str, Any] = _deep_merge(DEFAULTS, {})

    config_path = path or os.environ.get("PRIVYX_CONFIG")
    if config_path:
        merged = _deep_merge(merged, _load_yaml(Path(config_path)))

    merged = _deep_merge(merged, env_config())

    if extra:
        merged = _deep_merge(merged, extra)

    try:
        return Settings(**merged)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid configuration: {exc}") from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping: {path}")
    return data