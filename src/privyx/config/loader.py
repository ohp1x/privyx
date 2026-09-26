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
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    path: str | Path | None = None,
    *,
    extra: dict[str, Any] | None = None,
    base_extra: dict[str, Any] | None = None,
) -> Settings:
    """Load and merge configuration into validated :class:`Settings`.

    Precedence, low → high: built-in defaults < ``base_extra`` < YAML file <
    environment < ``extra``.  ``base_extra`` is for caller *defaults* that the
    user must still be able to override (e.g. ``privyx run`` preferring a
    per-conversation session); ``extra`` is for caller *overrides* that win over
    the user's config (e.g. ``--upstream``).
    """
    merged: dict[str, Any] = _deep_merge(DEFAULTS, base_extra or {})

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
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file is not valid YAML: {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping: {path}")
    return data
