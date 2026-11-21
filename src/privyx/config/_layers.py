"""Configuration loader: merges defaults, YAML files, and environment.

Precedence (low → high):
    1. Built-in defaults
    2. Caller defaults (``base_extra``)
    3. ``~/.privyx/config.yaml``
    4. ``privyx.yaml``, then ``.privyx/config.yaml``, in the working directory,
       each only once trusted
    5. Environment variables (``PRIVYX_*``)
    6. Caller overrides such as command-line flags (``extra``)

A file named with ``--config`` or ``PRIVYX_CONFIG`` is read alone, in place of
3 and 4.  The files a config file names in ``include`` come just below it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.config._files import _config_layers, _config_paths
from privyx.config._interpolate import _DEFERRED, _interpolate, _settle
from privyx.config._presets import (
    CHOSEN_PRESETS,
    _check_overrides,
    _check_presets,
    _preset_layers,
)
from privyx.config._unused import _UNUSED_WARNED, _raise_unknown, _unknown_keys, _unused
from privyx.config.agents import resolve_profiles
from privyx.config.defaults import DEFAULTS
from privyx.config.env import env_config
from privyx.config.schema import PLUGGABLE, Settings
from privyx.core import ConfigError
from privyx.security.secrets import resolve_secrets

if TYPE_CHECKING:
    from privyx.core import Registry

_log = logging.getLogger("privyx")


class _Types:
    """The built-in types of each section, read from the app's registries.

    A built-in type's options are checked when the config is read; a plugin's
    when it is built, since the plugins a config loads are not loaded yet.
    Only the chosen types are imported, and the others of a section only when
    a key is not one of the chosen type's.
    """

    def __init__(self, registries: Mapping[str, Registry[Any]]) -> None:
        self._registries = registries
        self._unions: dict[str, frozenset[str]] = {}

    def builtin(self, section: str, kind: object) -> bool:
        """Whether ``kind`` is a built-in type of ``section``."""
        registry = self._registries.get(section)
        return isinstance(kind, str) and registry is not None and registry.is_builtin(kind)

    def fields(self, section: str, kind: str) -> frozenset[str]:
        """The keys built-in type ``kind`` takes: ``type``, the shared ones, and its options."""
        options = self._registries[section].get(kind).Options
        return frozenset({*PLUGGABLE[section].model_fields, *options.model_fields})

    def union(self, section: str) -> frozenset[str]:
        """The keys some built-in type of ``section`` takes."""
        if section not in self._unions:
            builtins = self._registries[section].builtin_names()
            self._unions[section] = frozenset(PLUGGABLE[section].model_fields).union(
                *(self.fields(section, kind) for kind in builtins)
            )
        return self._unions[section]

    def checked(self, section: str, data: Any, setting: str) -> Any:
        """``data`` with a built-in type's options checked and their defaults filled in.

        The keys another built-in type takes, which :func:`_unused` let
        through from another layer, are dropped.

        Raises:
            ConfigError: If an option has an invalid value.
        """
        if not isinstance(data, dict):
            return data  # the wrong shape is pydantic's to report
        kind = data.get("type", PLUGGABLE[section].model_fields["type"].default)
        if not self.builtin(section, kind):
            return data
        own = self.fields(section, kind)
        options = {key: value for key, value in data.items() if key != "type" and key in own}
        checked = self._registries[section].validate(kind, options, setting=setting)
        return {"type": kind, **checked.model_dump()}


def _deep_merge(
    base: dict[str, Any], override: dict[str, Any], types: _Types, *, top: bool = True
) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        section = key if top and key in PLUGGABLE else None
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(
                _retyped(section, current, value, types), value, types, top=False
            )
        elif key == "detector" and isinstance(current, list) and isinstance(value, dict):
            # A section given as a list (`detector: [...]`) takes a mapping
            # override such as PRIVYX_DETECTOR_CACHE into each item.  Replacing
            # the list would silently drop the detectors it names.
            result[key] = [
                _deep_merge(_retyped(section, item, value, types), value, types, top=False)
                if isinstance(item, dict)
                else item
                for item in current
            ]
        else:
            result[key] = value
    return result


def _retyped(
    section: str | None, current: dict[str, Any], override: dict[str, Any], types: _Types
) -> dict[str, Any]:
    """``current`` without a plugin's options when ``override`` picks another ``type``.

    They are options of the type below, and would be unknown settings, or wrong
    ones, for the new type.  The keys some built-in type takes are merged as
    before.  Below the built-in default type, every key is kept: it was written
    for the type a later layer picks, as with ``vault: {bucket: b}`` in a file
    and ``PRIVYX_VAULT`` naming the plugin.
    """
    if section is None or "type" not in override:
        return current
    default = PLUGGABLE[section].model_fields["type"].default
    below = current.get("type", default)
    if below in (override["type"], default):
        return current
    fields = types.union(section)
    return {key: value for key, value in current.items() if key in fields}


def load_config(
    path: str | Path | None = None,
    *,
    extra: dict[str, Any] | None = None,
    base_extra: dict[str, Any] | None = None,
    agent: str | None = None,
    presets: Sequence[str] | None = None,
    sources: dict[str, str] | None = None,
    secrets: bool = True,
    warnings: list[str] | None = None,
    types: Mapping[str, Registry[Any]],
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

    With ``agent``, that profile's provider/upstream are applied within each
    layer, above that layer's common settings. Later layers still win. Its
    ``presets``, then its ``privyx`` settings, then ``presets`` come just
    below ``extra``; without a profile, just ``presets``.  ``presets`` defaults
    to :data:`CHOSEN_PRESETS`.
    ``sources`` optionally receives dotted field paths -> winning layer.
    ``secrets=False`` leaves secret references (``op://``, ``keychain://``)
    as written, so no password manager is asked.
    A setting a section's type does not use (:func:`_unused`) is an error when
    written beside the type; masking keys from another layer that no detector
    uses are logged, once per process, or appended to ``warnings`` when it is
    given.
    ``types`` holds the registry of each section's components, by section
    name: a built-in type's options are checked against its ``Options``, and
    a section of one gets their defaults.  ``privyx.load_config`` passes the
    built-in ones.
    """
    if presets is None:
        presets = CHOSEN_PRESETS.get()
    known = _Types(types)
    merged: dict[str, Any] = {}
    origins: dict[str, str] = {}
    layers: list[tuple[str, dict[str, Any]]] = []

    def apply(data: dict[str, Any], source: str) -> None:
        nonlocal merged
        layers.append((source, data))
        _record_sources(data, source, origins, base=merged)
        merged = _deep_merge(merged, data, known)

    apply(DEFAULTS, "defaults")
    apply(base_extra or {}, "run defaults")

    config_files, untrusted = _config_paths(path)
    for config_file in untrusted:
        _log.warning(
            "ignoring %s: not trusted, or changed since; run `privyx trust` to use it",
            config_file,
        )
    for config_file in config_files:
        for file, data in _config_layers(config_file, warn=True):
            apply(_interpolate(data, file), str(file.resolve()))

    apply(env_config(), "environment")

    if extra:
        apply(extra, "CLI")

    # A misspelled key would otherwise be dropped without a word, and the
    # setting it meant to change (a detector's terms, say) silently not apply.
    _raise_unknown(_unknown_keys(Settings, merged, known))
    _check_presets(merged, presets, known)

    profiles = merged.get("agents")
    if isinstance(profiles, dict):
        _apply_colon_inheritance(profiles)
        profiles, inherited_sources = resolve_profiles(profiles, origins, known)
        _check_overrides(profiles, known)
        if agent in profiles:
            profiles[agent] = _settle(profiles[agent], expand=True)
        selected = profiles.get(agent) if agent else None
        own = selected.get("presets") if isinstance(selected, dict) else None
        override = selected.get("privyx") if isinstance(selected, dict) else None
        patches = _preset_layers(merged, own if isinstance(own, list) else [])
        if isinstance(override, dict) and override:
            patches.append((f"agents.{agent}.privyx", override))
        patches += _preset_layers(merged, presets)
        at = next((i for i, (source, _) in enumerate(layers) if source == "CLI"), len(layers))
        layers[at:at] = patches
        # Reapply transport fields at their originating layer, so a late
        # common environment/CLI override still beats an inherited profile.
        effective: dict[str, Any] = {}
        effective_sources: dict[str, str] = {}
        for source, data in layers:
            projection = {}
            if isinstance(selected, dict):
                for field in ("provider", "upstream"):
                    if inherited_sources.get(f"agents.{agent}.{field}") == source:
                        projection[field] = selected[field]
            layer = _agent_layer(data, agent, projection, known)
            _record_sources(layer, source, effective_sources, base=effective)
            effective = _deep_merge(effective, layer, known)
        effective["agents"] = profiles
        effective_sources.update(inherited_sources)
        merged, origins = effective, effective_sources

    if sources is not None:
        sources.clear()
        sources.update(origins)
    refused, unused = _unused(layers, merged, origins, known)
    if refused:
        raise ConfigError("; ".join(refused))

    merged = _settle(merged, expand=False)
    # Another profile's deferred settings keep their secret references as
    # written, so a command does not open a password manager for them.
    held: dict[str, dict[str, Any]] = {}
    agents = merged.get("agents")
    if isinstance(agents, dict):
        for name, profile in agents.items():
            if name != agent and isinstance(profile, dict):
                held[name] = {key: profile.pop(key) for key in _DEFERRED if key in profile}
    # So are the presets: the ones in use were applied above.
    definitions = merged.pop("presets", None)
    if secrets:
        merged = resolve_secrets(merged)
    for name, values in held.items():
        merged["agents"][name].update(values)
    if definitions is not None:
        merged["presets"] = definitions
    for name in PLUGGABLE:
        value = merged.get(name)
        if isinstance(value, list):
            merged[name] = [
                known.checked(name, item, f"{name}[{i}]") for i, item in enumerate(value)
            ]
        elif name in merged:
            merged[name] = known.checked(name, value, name)
    try:
        settings = Settings(**merged)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid configuration: {exc}") from exc
    if warnings is not None:
        warnings.extend(unused)
    for warning in unused:
        if warnings is None and warning not in _UNUSED_WARNED:
            _UNUSED_WARNED.add(warning)
            _log.warning("%s", warning)
    return settings


def _apply_colon_inheritance(profiles: dict[str, Any]) -> None:
    """Make ``agent:profile`` a variant of ``agent`` when it says nothing else.

    A profile named ``base:variant`` with no explicit ``extends`` and a ``base``
    that is itself a profile inherits from it, so a one-line ``claude:work``
    needs no ``command`` or ``extends``. An explicit ``extends`` is left as the
    user wrote it, including one naming another ``:`` profile.
    """
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or ":" not in name:
            continue
        base = name.split(":", 1)[0]
        if base in profiles and not profile.get("extends"):
            profile["extends"] = base


def _agent_layer(
    data: dict[str, Any], agent: str | None, profile: dict[str, Any], types: _Types
) -> dict[str, Any]:
    if agent is None:
        return data
    overrides: dict[str, Any] = {}
    provider = data.get("provider")
    if isinstance(provider, dict) and "base_url" in provider and "upstream_url" not in data:
        # In a run plan these name the same destination. A later file's common
        # base URL must be able to override an earlier profile's upstream.
        overrides["upstream_url"] = provider["base_url"]
    if "provider" in profile:
        overrides["provider"] = {"type": profile["provider"]}
    if "upstream" in profile:
        overrides["upstream_url"] = profile["upstream"]
        overrides.setdefault("provider", {})["base_url"] = profile["upstream"]
    return _deep_merge(data, overrides, types)


def _record_sources(
    data: dict[str, Any],
    source: str,
    sources: dict[str, str],
    prefix: str = "",
    *,
    base: dict[str, Any] | None = None,
) -> None:
    for key, value in data.items():
        path = f"{prefix}{key}"
        current = (base or {}).get(key)
        if isinstance(value, dict):
            if key == "detector" and isinstance(current, list):
                # Match the mapping override that _deep_merge applies to each detector.
                for index, item in enumerate(current):
                    if isinstance(item, dict):
                        _record_sources(value, source, sources, f"{path}.{index}.", base=item)
            else:
                if not isinstance(current, dict):
                    for old in list(sources):
                        if old == path or old.startswith(path + "."):
                            del sources[old]
                _record_sources(
                    value,
                    source,
                    sources,
                    path + ".",
                    base=current if isinstance(current, dict) else None,
                )
        else:
            # A replaced list/scalar invalidates any old nested provenance.
            for old in list(sources):
                if old.startswith(path + "."):
                    del sources[old]
            sources[path] = source
            if key == "cache" and isinstance(value, bool):
                # Detector cache shorthand sets enabled; max_size still uses its default.
                sources[path + ".enabled"] = source
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        _record_sources(item, source, sources, f"{path}.{index}.")
