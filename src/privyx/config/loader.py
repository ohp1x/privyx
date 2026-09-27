"""Configuration loader — merges defaults, YAML file, and environment.

Precedence (low → high):
    1. Built-in defaults
    2. YAML config file (``--config`` or ``PRIVYX_CONFIG``)
    3. Environment variables
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any, get_args

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from privyx.config.defaults import DEFAULTS
from privyx.config.env import env_config
from privyx.config.schema import PluggableConfig, Settings
from privyx.core.errors import ConfigError


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        elif isinstance(current, list) and isinstance(value, dict):
            # A section given as a list (`detector: [...]`) takes a mapping
            # override such as PRIVYX_DETECTOR_CACHE into each item.  Replacing
            # the list would silently drop the detectors it names.
            result[key] = [
                _deep_merge(item, value) if isinstance(item, dict) else item for item in current
            ]
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

    # A misspelled key would otherwise be dropped without a word, and the
    # setting it meant to change (a detector's terms, say) silently not apply.
    unknown = _unknown_keys(Settings, merged)
    if unknown:
        plural = "s" if len(unknown) > 1 else ""
        raise ConfigError(f"unknown setting{plural} {', '.join(unknown)}")

    try:
        return Settings(**merged)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid configuration: {exc}") from exc


def _unknown_keys(model: type[BaseModel], data: Any, path: str = "") -> list[str]:
    """Name each key in ``data`` that ``model`` does not define, with a suggestion.

    Nested sections and ``detector`` lists are walked too.  A pluggable section
    whose ``type`` is not built in is skipped: its extra keys are the plugin's
    options.  Only key paths are reported, never values — a typo'd key may hold
    a secret.
    """
    if not isinstance(data, dict):
        return []  # the wrong shape is pydantic's to report
    fields = model.model_fields
    if issubclass(model, PluggableConfig):
        kind = data.get("type", fields["type"].default)
        if not (isinstance(kind, str) and kind in model.builtin_types):
            return []
    unknown = []
    for key, value in data.items():
        name = str(key)
        if name not in fields:
            match = difflib.get_close_matches(name, list(fields), n=1)
            hint = f" (did you mean {match[0]!r}?)" if match else ""
            unknown.append(f"{path + name!r}{hint}")
            continue
        section = _section(fields[name].annotation)
        if section is None:
            continue
        if isinstance(value, list):
            for i, item in enumerate(value):
                unknown += _unknown_keys(section, item, f"{path}{name}[{i}].")
        else:
            unknown += _unknown_keys(section, value, f"{path}{name}.")
    return unknown


def _section(annotation: Any) -> type[BaseModel] | None:
    """The settings model in a field's annotation (``X``, ``X | bool``, ``list[X]``)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _section(arg)
        if found is not None:
            return found
    return None


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
