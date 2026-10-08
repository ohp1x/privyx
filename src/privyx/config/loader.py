"""Configuration loader — merges defaults, YAML files, and environment.

Precedence (low → high):
    1. Built-in defaults
    2. Caller defaults (``base_extra``)
    3. ``~/.privyx/config.yaml``
    4. ``privyx.yaml``, then ``.privyx/config.yaml``, in the working directory,
       each only once trusted
    5. Environment variables (``PRIVYX_*``)
    6. Caller overrides such as command-line flags (``extra``)

A file named with ``--config`` or ``PRIVYX_CONFIG`` is read alone, in place of
3 and 4.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, get_args

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from privyx.config.defaults import DEFAULTS
from privyx.config.env import discovery_disabled, env_config
from privyx.config.schema import PluggableConfig, Settings
from privyx.core.errors import ConfigError

_log = logging.getLogger("privyx")


def _home_file(name: str) -> Path | None:
    """``~/.privyx/<name>``, or ``None`` for a user without a home directory.

    Never a path left relative: a project could then supply the file itself.
    """
    try:
        return Path.home() / ".privyx" / name
    except RuntimeError:
        return None


def find_local_configs() -> list[Path]:
    """The working directory's own config files, low → high precedence.

    ``privyx.yaml``, then ``.privyx/config.yaml``.  In the home directory the
    second is ``~/.privyx/config.yaml`` itself, and is left out.
    """
    cwd = Path.cwd()
    home_config = _home_file("config.yaml")
    return [
        path
        for path in (cwd / "privyx.yaml", cwd / ".privyx" / "config.yaml")
        if path.is_file() and not (home_config and path.resolve() == home_config.resolve())
    ]


def _fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc


def _trusted() -> dict[str, str]:
    """The files ``privyx trust`` recorded: resolved path → SHA-256 of the content."""
    store = _home_file("trusted.json")
    try:
        trusted = json.loads(store.read_text(encoding="utf-8")) if store else {}
    except (OSError, ValueError):
        return {}  # nothing trusted yet, or a record that cannot be read
    return trusted if isinstance(trusted, dict) else {}


def is_trusted(path: Path) -> bool:
    """Whether ``path`` was trusted with the content it has now."""
    return _trusted().get(str(path.resolve())) == _fingerprint(path)


def trust(path: Path) -> None:
    """Record ``path``, with the content it has now, as trusted.

    A config file found in the working directory can load plugins and choose
    the upstream, so one that came with a project is read only on the user's
    word, given again after each change to it.
    """
    store = _home_file("trusted.json")
    if store is None:
        raise ConfigError("cannot trust a config file: no home directory to record it in")
    trusted = {**_trusted(), str(path.resolve()): _fingerprint(path)}
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(trusted, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot write {store}: {exc.strerror or exc}") from exc


def _config_paths(path: str | Path | None) -> tuple[list[Path], list[Path]]:
    """The config files to read, low → high precedence, and the local ones not trusted."""
    named = path or os.environ.get("PRIVYX_CONFIG")
    if named:
        return [Path(named)], []
    if discovery_disabled():
        return [], []
    home_config = _home_file("config.yaml")
    active = [home_config] if home_config and home_config.is_file() else []
    untrusted: list[Path] = []
    for local in find_local_configs():
        (active if is_trusted(local) else untrusted).append(local)
    return active, untrusted


def active_config_paths(path: str | Path | None = None) -> list[Path]:
    """The config files :func:`load_config` reads for ``path``, low → high precedence."""
    return _config_paths(path)[0]


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

    Precedence, low → high: built-in defaults < ``base_extra`` < YAML files <
    environment < ``extra``.  ``base_extra`` is for caller *defaults* that the
    user must still be able to override (e.g. ``privyx run`` preferring a
    per-conversation session); ``extra`` is for caller *overrides* that win over
    the user's config (e.g. ``--upstream``).

    The YAML files are ``path`` (or ``PRIVYX_CONFIG``) alone when one is named;
    otherwise ``~/.privyx/config.yaml`` and then the working directory's trusted
    ``privyx.yaml`` and ``.privyx/config.yaml``.
    """
    merged: dict[str, Any] = _deep_merge(DEFAULTS, base_extra or {})

    config_files, untrusted = _config_paths(path)
    for config_file in untrusted:
        _log.warning(
            "ignoring %s: not trusted, or changed since; run `privyx trust` to use it",
            config_file,
        )
    for config_file in config_files:
        merged = _deep_merge(merged, _load_yaml(config_file))

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
