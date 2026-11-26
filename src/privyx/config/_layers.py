"""Configuration loader: merges defaults, YAML files, and environment.

Precedence (low → high):
    1. Built-in defaults
    2. The defaults plugins add (``Privyx.defaults``), then the caller's
       (``base_extra``)
    3. ``~/.privyx/config.yaml``, then ``~/.privyx/settings.yaml``
    4. ``privyx.yaml``, ``.privyx/config.yaml``, then ``.privyx/settings.yaml``,
       in the working directory, each only once trusted
    5. Environment variables (``PRIVYX_*``)
    6. Caller overrides such as command-line flags (``extra``)

A file named with ``--config`` or ``PRIVYX_CONFIG`` is read alone, in place of
3 and 4.  The files a config file names in ``include`` come just below it.
"""

from __future__ import annotations

import difflib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from privyx.config._env import ALIASES, env_config
from privyx.config._files import _config_layers, _config_paths, key_line
from privyx.config._interpolate import _DEFERRED, _interpolate, _settle
from privyx.config._keys import env_name, leaves
from privyx.config._migrate import (
    OLD_OPERATOR_KEYS,
    translate_layer,
    translate_operators,
    without_old_operator_keys,
)
from privyx.config._presets import (
    CHOSEN_PRESETS,
    _check_overrides,
    _check_presets,
    _preset_layers,
)
from privyx.config._unused import _UNUSED_WARNED, _raise_unknown, _unknown_keys, _unused
from privyx.config.agents import profile_type, resolve_profiles
from privyx.config.defaults import DEFAULTS
from privyx.config.providers import Resolved, resolve_provider, token_warnings
from privyx.config.schema import NAMED, PLUGGABLE, AgentConfig, PluggableConfig, Settings
from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.core import Registry

_log = logging.getLogger("privyx")


#: The settings model of one component of each kind.
MODELS: dict[str, type[PluggableConfig]] = {
    **PLUGGABLE,
    **{kind: model for kind, model in NAMED.values()},
}

#: The settings of an entry that Privyx applies itself, never its type's options.
_CORE = ("type", "enabled")


class _Types:
    """The built-in types of each kind, read from the app's registries.

    A built-in type's options are checked when the config is read; a plugin's
    when it is built, since the plugins a config loads are not loaded yet.
    Only the chosen types are imported, and the others of a kind only when
    a key is not one of the chosen type's.
    """

    def __init__(self, registries: Mapping[str, Registry[Any]]) -> None:
        self._registries = registries
        self._unions: dict[str, frozenset[str]] = {}

    def registry(self, kind: str) -> Registry[Any] | None:
        """The registry of ``kind``, such as ``agent``, if the app has one."""
        return self._registries.get(kind)

    def builtin(self, kind: str, name: object) -> bool:
        """Whether ``name`` is a built-in type of ``kind``."""
        registry = self._registries.get(kind)
        return isinstance(name, str) and registry is not None and registry.is_builtin(name)

    def fields(self, kind: str, name: str) -> frozenset[str]:
        """The keys built-in type ``name`` takes: ``type``, the shared ones, and its options."""
        options = self._registries[kind].get(name).Options
        return frozenset({*MODELS[kind].model_fields, *options.model_fields})

    def union(self, kind: str) -> frozenset[str]:
        """The keys some built-in type of ``kind`` takes."""
        if kind not in self._unions:
            builtins = self._registries[kind].builtin_names()
            self._unions[kind] = frozenset(MODELS[kind].model_fields).union(
                *(self.fields(kind, name) for name in builtins)
            )
        return self._unions[kind]

    def checked(
        self,
        kind: str,
        data: Any,
        setting: str,
        default: str,
        origin: Callable[[str], str] | None = None,
    ) -> Any:
        """``data`` with a built-in type's options checked and their defaults filled in.

        ``default`` is the type without a ``type`` key.  The keys another
        built-in type takes, which :func:`_unused` let through from another
        layer, are dropped.  ``origin`` names where an invalid value was given.

        Raises:
            ConfigError: If an option has an invalid value.
        """
        if not isinstance(data, dict):
            return data  # the wrong shape is pydantic's to report
        name = data.get("type", default)
        if not self.builtin(kind, name):
            return data
        own = self.fields(kind, name)
        options = {k: v for k, v in data.items() if k not in _CORE and k in own}
        checked = self._registries[kind].validate(name, options, setting=setting, origin=origin)
        core = {k: data[k] for k in _CORE[1:] if k in data}
        return {"type": name, **core, **checked.model_dump()}


def _deep_merge(
    base: dict[str, Any], override: dict[str, Any], types: _Types, *, top: bool = True
) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            if top and key in NAMED:
                kind = NAMED[key][0]
                merged = dict(current)
                for name, entry in value.items():
                    below = merged.get(name)
                    if isinstance(below, dict) and isinstance(entry, dict):
                        below = _retyped(kind, name, below, entry, types)
                        merged[name] = _deep_merge(below, entry, types, top=False)
                    else:
                        merged[name] = entry
                result[key] = merged
            else:
                section = key if top and key in PLUGGABLE else None
                if section is not None:
                    default = PLUGGABLE[section].model_fields["type"].default
                    current = _retyped(section, default, current, value, types)
                result[key] = _deep_merge(current, value, types, top=False)
        else:
            result[key] = value
    return result


def _retyped(
    kind: str, default: str, current: dict[str, Any], override: dict[str, Any], types: _Types
) -> dict[str, Any]:
    """``current`` without a plugin's options when ``override`` picks another ``type``.

    They are options of the type below, and would be unknown settings, or wrong
    ones, for the new type.  The keys some built-in type takes are merged as
    before.  Below the ``default`` type, every key is kept: it was written
    for the type a later layer picks, as with ``vault: {bucket: b}`` in a file
    and ``PRIVYX_VAULT`` naming the plugin.
    """
    if "type" not in override:
        return current
    below = current.get("type", default)
    if below in (override["type"], default):
        return current
    fields = types.union(kind)
    if kind == "operator" and types.builtin(kind, below) and types.builtin(kind, override["type"]):
        fields |= OLD_OPERATOR_KEYS  # judged once the type in use is known
    return {key: value for key, value in current.items() if key in fields}


def _named(merged: dict[str, Any]) -> None:
    """Give each entry of ``detectors`` and ``features`` without a ``type`` its name's, in place."""
    for section in NAMED:
        entries = merged.get(section)
        if isinstance(entries, dict):
            merged[section] = {
                name: {"type": name, **entry}
                if isinstance(entry, dict) and "type" not in entry
                else entry
                for name, entry in entries.items()
            }


@dataclass
class Loaded:
    """A config read: its settings, where each came from, and what to say about it."""

    settings: Settings
    #: Each setting's dotted path, with the layer that won.
    sources: dict[str, str]
    #: Old keys, and settings that have no effect: names, never values.
    warnings: list[str]
    #: The layers read, lowest first.
    layers: list[tuple[str, dict[str, Any]]]

    @cached_property
    def written(self) -> dict[str, list[str]]:
        """Each setting's dotted path, with every layer that writes it, lowest first."""
        found: dict[str, list[str]] = {}
        for source, data in self.layers:
            for leaf in leaves(data):
                found.setdefault(".".join(leaf), []).append(source)
        return found

    def log_warnings(self) -> Loaded:
        """Log each warning, once per process."""
        for warning in self.warnings:
            if warning not in _UNUSED_WARNED:
                _UNUSED_WARNED.add(warning)
                _log.warning("%s", warning)
        return self


def load_config(path: str | Path | None = None, **options: Any) -> Settings:
    """The settings of :func:`read_config`, its warnings logged once per process."""
    return read_config(path, **options).log_warnings().settings


def read_config(
    path: str | Path | None = None,
    *,
    extra: dict[str, Any] | None = None,
    base_extra: dict[str, Any] | None = None,
    agent: str | None = None,
    presets: Sequence[str] | None = None,
    secrets: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    setup: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    defaults_only: bool = False,
    types: Mapping[str, Registry[Any]],
) -> Loaded:
    """Load and merge configuration into validated :class:`Settings`.

    Precedence, low → high: built-in defaults < ``base_extra`` < YAML files <
    environment < ``extra``.  ``base_extra`` is for caller *defaults* that the
    user must still be able to override (e.g. ``privyx run`` preferring a
    per-conversation session); ``extra`` is for caller *overrides* that win over
    the user's config (e.g. ``--upstream``).

    The YAML files are ``path`` (or ``PRIVYX_CONFIG``) alone when one is named;
    otherwise ``config.yaml`` and ``settings.yaml`` in ``~/.privyx``, then the
    working directory's trusted ``privyx.yaml``, ``.privyx/config.yaml``, and
    ``.privyx/settings.yaml``.

    With ``agent``, that profile's provider/upstream are applied within each
    layer, above that layer's common settings. Later layers still win. Its
    ``presets``, then its ``privyx`` settings, then ``presets`` come just
    below ``extra``; without a profile, just ``presets``.  ``presets`` defaults
    to :data:`CHOSEN_PRESETS`.
    ``secrets`` resolves the secret references of the merged config; without
    it they are left as written (``op://...``), so no password manager is asked.
    ``setup``, given the merged files, environment, and ``extra``, sets up
    the plugins they name and returns the defaults those add (agent profiles,
    presets): when there are some, the config is read again with them just
    above Privyx's own.  A plugin a preset or a profile names is not set up here.
    A setting a section's type does not use (:func:`_unused`) is an error when
    written beside the type; masking keys from another layer that no detector
    uses are warnings.
    ``defaults_only`` reads no file and no environment: the settings as
    they are when nothing is written.
    ``types`` holds the registry of each section's components, by section
    name: a built-in type's options are checked against its ``Options``, and
    a section of one gets their defaults.  ``privyx.load_config`` passes the
    built-in ones.
    """
    if presets is None:
        presets = CHOSEN_PRESETS.get()
    known = _Types(types)
    read = _read_layers(path, extra, base_extra, None, defaults_only, known)
    if setup is not None and (added := setup(read.merged)):
        read = _read_layers(path, extra, base_extra, added, defaults_only, known)
    layers, deprecated = read.layers, read.deprecated

    merged = read.merged
    _named(merged)
    # A misspelled key would otherwise be dropped without a word, and the
    # setting it meant to change (a detector's terms, say) silently not apply.
    # The old operator keys wait for the type in use, which a profile may pick.
    is_builtin = partial(known.builtin, "operator")
    checked = without_old_operator_keys(merged)
    _raise_unknown(_unknown_keys(Settings, checked, known))
    _check_presets(checked, presets, known)

    profiles, inherited = _apply_profile(merged, layers, read.origins, agent, presets, known)
    layers[:] = translate_operators(layers, is_builtin, deprecated)
    merged, origins, resolved = _effective(layers, profiles, inherited, agent, known)
    _raise_unknown(_unknown_keys(Settings, {"operator": merged.get("operator")}, known))
    refused, unused = _unused(layers, merged, origins, known)
    if refused:
        raise ConfigError("; ".join(refused))
    warnings = (
        list(dict.fromkeys(deprecated))
        + resolved.warnings
        + token_warnings(merged, origins)
        + unused
    )
    settings = _validated(_with_secrets(merged, agent, secrets), known, partial(_given, origins))
    settings._home = resolved.home
    return Loaded(settings, origins, warnings, layers)


@dataclass
class _Read:
    """The config's layers, lowest first, each with its old keys written the new way."""

    layers: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    merged: dict[str, Any] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)
    deprecated: list[str] = field(default_factory=list)


def _read_layers(
    path: str | Path | None,
    extra: dict[str, Any] | None,
    base_extra: dict[str, Any] | None,
    defaults: dict[str, Any] | None,
    defaults_only: bool,
    known: _Types,
) -> _Read:
    """Every layer from the defaults to ``extra``, ``defaults`` being what plugins add."""
    read = _Read()

    def apply(data: dict[str, Any], source: str) -> None:
        below = read.merged.get("detectors")
        data = translate_layer(
            data, below if isinstance(below, dict) else {}, source, read.deprecated
        )
        read.layers.append((source, data))
        _record_sources(data, source, read.origins, base=read.merged)
        read.merged = _deep_merge(read.merged, data, known)

    apply(DEFAULTS, "defaults")
    if defaults:
        apply(defaults, "plugin defaults")
    apply(base_extra or {}, "run defaults")

    config_files, untrusted = ([], []) if defaults_only else _config_paths(path)
    # Read again with the plugins' defaults: what was said the first time stands.
    warn = defaults is None
    for config_file in untrusted if warn else ():
        _log.warning(
            "ignoring %s: not trusted, or changed since; run `privyx config trust` to use it",
            config_file,
        )
    for config_file in config_files:
        for file, data in _config_layers(config_file, warn=warn):
            apply(_interpolate(data, file), str(file.resolve()))

    if not defaults_only:
        apply(env_config(read.merged, known), "environment")
    if extra:
        apply(extra, "CLI")
    return read


def _apply_profile(
    merged: dict[str, Any],
    layers: list[tuple[str, dict[str, Any]]],
    origins: dict[str, str],
    agent: str | None,
    presets: Sequence[str],
    known: _Types,
) -> tuple[Any, dict[str, str]]:
    """The profiles resolved, and the layer of each field a profile has.

    The presets and ``privyx`` settings that apply go into ``layers``, just
    below the command line.
    """
    profiles = merged.get("agents")
    if not isinstance(profiles, dict):
        return profiles, {}
    _apply_colon_inheritance(profiles)
    profiles, inherited = resolve_profiles(profiles, origins, known)
    _check_overrides(without_old_operator_keys({"agents": profiles})["agents"], known)
    _check_types(profiles, known, partial(_given, {**origins, **inherited}))
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
    return profiles, inherited


def _effective(
    layers: list[tuple[str, dict[str, Any]]],
    profiles: Any,
    inherited: dict[str, str],
    agent: str | None,
    known: _Types,
) -> tuple[dict[str, Any], dict[str, str], Resolved]:
    """The settings merged with the profile's in place, their origins, and the provider."""
    selected = profiles.get(agent) if agent and isinstance(profiles, dict) else None
    # The selected profile's provider and upstream, at the layer each comes from.
    projections: dict[str, dict[str, Any]] = {}
    if isinstance(selected, dict):
        for name in ("provider", "upstream"):
            layer = inherited.get(f"agents.{agent}.{name}")
            if layer is not None:
                projections.setdefault(layer, {})[name] = selected[name]
    # Where requests go is the provider's, chosen across the layers below; the
    # rest is merged again here, so the profile's settings land at their layer.
    effective: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for source, data in layers:
        rest = {k: v for k, v in data.items() if k not in ("provider", "providers", "upstream_url")}
        _record_sources(rest, source, origins, base=effective)
        effective = _deep_merge(effective, rest, known)
    resolved = resolve_provider(
        layers,
        projections,
        lambda current, mapping: _deep_merge({"provider": current}, {"provider": mapping}, known)[
            "provider"
        ],
        profile_layer=f"agents.{agent}.privyx",
    )
    effective["provider"] = resolved.provider
    effective["providers"] = resolved.providers
    effective["upstream_url"] = resolved.upstream_url
    origins.update(resolved.sources)
    if isinstance(profiles, dict):
        effective["agents"] = profiles
        origins.update(inherited)
    _named(effective)
    return effective, origins, resolved


def _with_secrets(
    merged: dict[str, Any],
    agent: str | None,
    secrets: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    """``merged`` with its deferred settings settled and its secret references resolved."""
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
    if secrets is not None:
        merged = secrets(merged)
    for name, values in held.items():
        merged["agents"][name].update(values)
    if definitions is not None:
        merged["presets"] = definitions
    return merged


def _validated(merged: dict[str, Any], known: _Types, origin: Callable[[str], str]) -> Settings:
    """The settings of ``merged``, each built-in type's options checked.

    Raises:
        ConfigError: If a value is invalid, naming its setting and where it
            was given (:func:`_given`), never the value.
    """
    named = merged.get("providers")
    if isinstance(named, dict):
        merged["providers"] = {
            name: known.checked("provider", item, f"providers.{name}", "generic", origin)
            for name, item in named.items()
        }
    for name, model in PLUGGABLE.items():
        if name in merged:
            default = model.model_fields["type"].default
            merged[name] = known.checked(name, merged[name], name, default, origin)
    for section, (kind, _) in NAMED.items():
        entries = merged.get(section)
        if isinstance(entries, dict):
            merged[section] = {
                name: known.checked(kind, entry, f"{section}.{name}", name, origin)
                for name, entry in entries.items()
            }
    try:
        return Settings(**merged)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_url=False):
            path = ".".join(str(part) for part in error["loc"])
            problems.append(f"invalid setting {path!r}{origin(path)}: {error['msg']}")
        raise ConfigError("; ".join(problems)) from exc


def _given(origins: Mapping[str, str], path: str) -> str:
    """Where setting ``path`` got its value, as `` (where)``: a file and line, or variables.

    Nothing for a default.  A list item or a key of a mapping is given where
    the setting holding it is.
    """
    parts = path.split(".")
    while len(parts) > 1 and ".".join(parts) not in origins:
        parts.pop()
    key = ".".join(parts)
    source = origins.get(key, "defaults")
    if source in ("defaults", "plugin defaults", "run defaults"):
        return ""
    if source == "environment":
        # As written: past `PRIVYX_`, a name is read in any case.
        names = {env_name(parts), *(name for name, aliased in ALIASES.items() if aliased == key)}
        given = [n for n in os.environ if n.startswith("PRIVYX_") and n.upper() in names]
        return f" ({', '.join(given) or source})"
    line = key_line(Path(source), parts) if Path(source).is_absolute() else None
    return f" ({source}:{line})" if line else f" ({source})"


def _check_types(
    profiles: dict[str, Any], types: _Types, origin: Callable[[str], str] | None = None
) -> None:
    """Give each profile its agent ``type``, and check the options the profile gives it.

    A type no plugin registered is left to fail when its profile is launched:
    the profile may belong to a plugin another config loads.  ``origin`` names
    where an invalid value was given.

    Raises:
        ConfigError: If an option is unknown to the type, or invalid.
    """
    registry = types.registry("agent")
    if registry is None:
        return
    names = registry.names()
    unknown: list[str] = []
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue  # the wrong shape is pydantic's to report
        kind = profile["type"] = profile_type(name, profile, names)
        if kind not in registry:
            continue
        options = {k: v for k, v in profile.items() if k not in AgentConfig.model_fields}
        own = list(registry.get(kind).Options.model_fields)
        for key in options:
            if key not in own:
                match = difflib.get_close_matches(key, [*AgentConfig.model_fields, *own], n=1)
                hint = f" (did you mean {match[0]!r}?)" if match else ""
                unknown.append(f"{f'agents.{name}.{key}'!r}{hint} for agent type {kind!r}")
        if not unknown:
            registry.validate(kind, options, setting=f"agents.{name}", origin=origin)
    _raise_unknown(unknown)


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
