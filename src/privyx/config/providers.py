"""Which provider a config selects, and where its credential may go.

A config names upstream connections under ``providers`` and selects one with
``provider: NAME``, or defines one of its own with a mapping there (the
inline one); a later layer's choice wins.  A profile selects its own with
``agents.<p>.provider``, which ``privyx run`` uses whatever the config or the
environment select at the top; ``--provider`` picks one over both.

A credential (``api_key``, or any header) belongs to the definition it is
written in, and goes only to the ``base_url`` that definition had when it
was written, or to its type's default.  ``PRIVYX_API_KEY`` and the common
``provider`` mapping belong to the provider selected above the profiles; a
profile that selects another one does not take them.  A destination from
elsewhere (a profile's ``upstream``, a common ``base_url`` while a profile
selects another provider) moves the requests, never the credential: the
provider refuses to start then (:func:`privyx.providers.check_credential`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from privyx.core import ConfigError

#: The fields that say where requests go, and how: a destination from
#: elsewhere carries them, never the credential.
_DESTINATION = (
    "base_url",
    "forward_client_auth",
    "path_mapping",
    "timeout",
    "connect_timeout",
    "max_connections",
)

#: The ``proxy`` keys that moved to the provider.
_MOVED_FROM_PROXY = _DESTINATION[1:]

#: The provider types whose key had a key of its own on the provider.
_TYPE_KEYS = ("openai", "anthropic", "google")

#: Operators that issue placeholder tokens, which ``token.*`` shapes.
TOKEN_OPERATORS = frozenset({"pseudonym", "hash", "encrypt"})


def translate_keys(layer: dict[str, Any], source: str, deprecated: list[str]) -> dict[str, Any]:
    """``layer`` with the provider and operator keys renamed in 0.3.0 written the new way.

    Each one a config file writes adds a warning to ``deprecated``; the
    environment's and the command line's are the current way to set them.
    Profiles' ``privyx`` settings and presets are translated too.
    """
    warn = source not in ("environment", "CLI", "defaults", "run defaults", "plugin defaults")
    layer = _translated(layer, "", deprecated if warn else [])
    presets = layer.get("presets")
    if isinstance(presets, dict):
        layer["presets"] = {
            name: _translated(dict(p), f"presets.{name}.", deprecated if warn else [])
            if isinstance(p, dict)
            else p
            for name, p in presets.items()
        }
    agents = layer.get("agents")
    if isinstance(agents, dict):
        for name, profile in agents.items():
            if isinstance(profile, dict) and isinstance(profile.get("privyx"), dict):
                own = _translated(
                    dict(profile["privyx"]), f"agents.{name}.privyx.", deprecated if warn else []
                )
                agents[name] = {**profile, "privyx": own}
    return layer


def _translated(data: dict[str, Any], prefix: str, deprecated: list[str]) -> dict[str, Any]:
    """``data``, one layer's settings or a patch of them, with the renamed keys moved."""

    def moved(old: str, new: str) -> None:
        deprecated.append(
            f"{prefix + old!r} is deprecated; write {prefix + new!r} (removed in 0.4.0)"
        )

    data = dict(data)
    if "upstream_url" in data:
        moved("upstream_url", "provider.base_url")  # still read as the base URL of the selection
    provider = data.get("provider")
    if isinstance(provider, dict):
        provider = dict(provider)
        for name in _TYPE_KEYS:
            key = f"{name}_api_key"
            if key in provider:
                value = provider.pop(key)
                moved(f"provider.{key}", f"providers.{name}.api_key")
                providers = data.setdefault("providers", {})
                providers[name] = {**providers.get(name, {}), "api_key": value}
        data["provider"] = provider
    proxy = data.get("proxy")
    if isinstance(proxy, dict) and any(key in proxy for key in _MOVED_FROM_PROXY):
        proxy = dict(proxy)
        moving = {key: proxy.pop(key) for key in _MOVED_FROM_PROXY if key in proxy}
        for key in moving:
            moved(f"proxy.{key}", f"provider.{key}")
        data["proxy"] = proxy
        if isinstance(provider, str):  # the layer selects one: its connection settings
            providers = data.setdefault("providers", {})
            providers[provider] = {**providers.get(provider, {}), **moving}
        else:
            data["provider"] = {**(provider or {}), **moving}
    operator = data.get("operator")
    if isinstance(operator, dict) and operator.get("type") == "hash":
        deprecated.append(
            f"{prefix + 'operator.type'!r} 'hash' is deprecated: anyone who guesses a value "
            "can match its token; use 'pseudonym' with 'anchor.secret' (removed in 0.4.0)"
        )
    return data


def translate_operator(
    data: dict[str, Any], kind: str, source: str, deprecated: list[str]
) -> dict[str, Any]:
    """``data`` with the operator settings renamed in 0.3.0 written the new way.

    Which ones are renamed depends on ``kind``, the operator type in use: a
    plugin's operator may have a ``length`` or a ``token`` of its own.
    Presets and profiles' ``privyx`` settings are translated with the type
    they set, if any.
    """
    warn = source not in ("environment", "CLI", "defaults", "run defaults", "plugin defaults")
    data = _operator_keys(data, kind, "", deprecated if warn else [])
    presets = data.get("presets")
    if isinstance(presets, dict):
        data["presets"] = {
            name: _operator_keys(
                dict(p), _kind(p, kind), f"presets.{name}.", deprecated if warn else []
            )
            if isinstance(p, dict)
            else p
            for name, p in presets.items()
        }
    agents = data.get("agents")
    if isinstance(agents, dict):
        agents = dict(agents)
        for name, profile in agents.items():
            own = profile.get("privyx") if isinstance(profile, dict) else None
            if isinstance(own, dict):
                prefix = f"agents.{name}.privyx."
                own = _operator_keys(
                    dict(own), _kind(own, kind), prefix, deprecated if warn else []
                )
                agents[name] = {**profile, "privyx": own}
        data["agents"] = agents
    return data


def _kind(patch: Mapping[str, Any], kind: str) -> str:
    """The operator type ``patch`` sets, or ``kind``."""
    operator = patch.get("operator")
    own = operator.get("type") if isinstance(operator, dict) else None
    return own if isinstance(own, str) else kind


def _operator_keys(
    data: dict[str, Any], kind: str, prefix: str, deprecated: list[str]
) -> dict[str, Any]:
    """``data`` with ``token.id_length``, and ``operator.length`` or ``.token``, moved."""

    def moved(old: str, new: str) -> None:
        deprecated.append(
            f"{prefix + old!r} is deprecated; write {prefix + new!r} (removed in 0.4.0)"
        )

    token = data.get("token")
    operator = data.get("operator")
    operator = dict(operator) if isinstance(operator, dict) else None
    if isinstance(token, dict) and "id_length" in token:
        token = dict(token)
        length = token.pop("id_length")
        data["token"] = token
        if kind in TOKEN_OPERATORS:
            moved("token.id_length", "operator.id_length")
            operator = {**(operator or {}), "id_length": length}
        else:
            deprecated.append(
                f"{prefix + 'token.id_length'!r} is deprecated, and operator type {kind!r} "
                "writes no id of a length; remove it (removed in 0.4.0)"
            )
    if operator is not None:
        for old, new, owner in (("length", "id_length", "hash"), ("token", "text", "redact")):
            if old in operator and kind == owner:
                moved(f"operator.{old}", f"operator.{new}")
                operator.setdefault(new, operator.pop(old))
        data["operator"] = operator
    return data


@dataclass
class Resolved:
    """The provider a config selects, as the loader hands it to the settings."""

    #: The definition in use, with a destination from elsewhere applied.
    provider: dict[str, Any]
    #: The named definitions, as written.
    providers: dict[str, dict[str, Any]]
    #: Where requests go when that is not the provider's own ``base_url``.
    upstream_url: str
    #: The ``base_url`` and ``type`` the credential was written with, if any.
    home: tuple[str, str] | None
    #: Dotted setting paths, each with the layer that set it.
    sources: dict[str, str] = field(default_factory=dict)
    #: Settings that have no effect, said without their values.
    warnings: list[str] = field(default_factory=list)


def resolve_provider(
    layers: Sequence[tuple[str, Mapping[str, Any]]],
    projections: Mapping[str, Mapping[str, Any]],
    merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    *,
    profile_layer: str = "",
) -> Resolved:
    """The provider ``layers`` select, and the home of its credential.

    Args:
        layers: The config's layers, lowest first.
        projections: The selected profile's ``provider`` and ``upstream``, by
            the layer each comes from.
        merge: Merges a definition with a layer's mapping for it.
        profile_layer: The layer of the profile's own ``privyx`` settings,
            whose ``provider`` is the profile's provider.

    Raises:
        ConfigError: If a provider named is not defined.
    """
    defs: dict[str, dict[str, Any]] = {"": {}}  # "" is the inline definition
    written: dict[str, dict[str, str]] = {"": {}}  # each definition's keys, and their layer
    home: dict[str, tuple[str, str]] = {}
    top = effective = ""
    named_at = ""
    # Once the profile has chosen, a choice at the top no longer moves the run.
    profiled = False
    dest: dict[str, Any] = {}
    dest_sources: dict[str, str] = {}

    def edit(name: str, mapping: Mapping[str, Any], source: str) -> bool:
        """Merge ``mapping`` into definition ``name``; whether it wrote a credential."""
        defs[name] = merge(defs.get(name, {}), dict(mapping))
        paths = written.setdefault(name, {})
        for path in _paths(mapping):
            paths[path] = source
        return bool(mapping.get("api_key")) or bool(mapping.get("headers"))

    for source, data in layers:
        keyed: set[str] = set()
        value = data.get("provider")
        own = source == profile_layer  # the profile's settings: its provider is theirs
        if isinstance(value, str) or (isinstance(value, dict) and "type" in value and not own):
            chosen = value if isinstance(value, str) else ""
            if not own:
                top = chosen
            if own or source == "CLI" or not profiled:
                effective = chosen
                named_at = source
                dest, dest_sources = {}, {}
        # The profile's own choice, above the layer's common one; the layer's
        # common URL below still sends it there, unless the profile has one too.
        projection = projections.get(source, {})
        if "provider" in projection:
            effective = str(projection["provider"])
            named_at = source
            profiled = True
            dest, dest_sources = {}, {}
        for name, mapping in (data.get("providers") or {}).items():
            if isinstance(mapping, dict) and edit(str(name), mapping, source):
                keyed.add(str(name))
        url = data.get("upstream_url")
        if isinstance(value, dict) or url:
            mapping = dict(value) if isinstance(value, dict) else {}
            if url:
                mapping["base_url"] = url
            target = effective if own else top
            if edit(target, mapping, source):
                keyed.add(target)
            if target != effective:
                # The profile's provider goes there, without this credential.
                moved = {key: mapping[key] for key in _DESTINATION if key in mapping}
                dest.update(moved)
                dest_sources.update(dict.fromkeys(_paths(moved), source))
        if "upstream" in projection:
            dest["base_url"] = projection["upstream"]
            dest_sources["base_url"] = source
        for name in keyed:
            definition = defs[name]
            home[name] = (str(definition.get("base_url") or ""), str(definition.get("type") or ""))

    if effective not in defs:
        import difflib

        named = sorted(name for name in defs if name)
        match = difflib.get_close_matches(effective, named, n=1)
        hint = f" (did you mean {match[0]!r}?)" if match else ""
        raise ConfigError(
            f"provider {effective!r} from {named_at} is not defined{hint}; "
            f"define it under 'providers' (defined: {', '.join(named)})"
        )
    definition = defs[effective]
    base = str(definition.get("base_url") or "")
    provider = {key: value for key, value in definition.items() if key != "base_url"}
    provider.update({key: value for key, value in dest.items() if key != "base_url"})
    provider["base_url"] = base
    sources = {f"provider.{path}": src for path, src in written.get(effective, {}).items()}
    for path, src in dest_sources.items():
        if path != "base_url":
            sources[f"provider.{path}"] = src
    sources["provider.type"] = named_at  # where the provider in use was chosen
    for name, paths in written.items():
        if name:
            sources.update({f"providers.{name}.{path}": src for path, src in paths.items()})
    upstream = str(dest.get("base_url") or "")
    if upstream:
        sources["upstream_url"] = dest_sources["base_url"]
    held = _held(definition)
    warnings = []
    kind = definition.get("type")
    if not effective and not held and kind in _TYPE_KEYS and _held(defs.get(str(kind), {})):
        warnings.append(
            f"the key of provider {kind!r} is not sent: the provider in use is the top-level "
            f"one of type {kind!r}; select the named one with 'provider: {kind}', or give the "
            "top-level one a key of its own"
        )
    return Resolved(
        provider=provider,
        providers={name: definition for name, definition in defs.items() if name},
        upstream_url=upstream,
        home=home.get(effective) if held else None,
        sources=sources,
        warnings=warnings,
    )


def token_warnings(merged: Mapping[str, Any], sources: Mapping[str, str]) -> list[str]:
    """A warning for each ``token`` setting an operator that issues no token leaves unused."""
    operator = merged.get("operator")
    kind = operator.get("type") if isinstance(operator, dict) else None
    if not isinstance(kind, str) or kind in TOKEN_OPERATORS:
        return []
    token = merged.get("token")
    return [
        f"'token.{key}' from {sources[f'token.{key}']} has no effect: operator type {kind!r} "
        "writes no placeholder tokens"
        for key in (token if isinstance(token, dict) else {})
        if sources.get(f"token.{key}", "defaults") != "defaults"
    ]


def _held(definition: Mapping[str, Any]) -> bool:
    """Whether ``definition`` holds a credential: a key, or a header, whose name says nothing."""
    headers = definition.get("headers")
    return bool(definition.get("api_key")) or (isinstance(headers, dict) and any(headers.values()))


def _paths(mapping: Mapping[str, Any], prefix: str = "") -> list[str]:
    """The dotted paths of the leaves of ``mapping``."""
    found: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, dict) and value:
            found += _paths(value, f"{prefix}{key}.")
        else:
            found.append(f"{prefix}{key}")
    return found
