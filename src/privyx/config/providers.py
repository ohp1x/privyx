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

from privyx.config._keys import leaves
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

#: The provider types that had a key setting of their own on the provider.
_TYPE_KEYS = ("openai", "anthropic", "google")

#: Operators that issue placeholder tokens, which ``token.*`` shapes.
TOKEN_OPERATORS = frozenset({"pseudonym", "hash", "encrypt"})


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
        for leaf in leaves(mapping):
            paths[".".join(leaf)] = source
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
                dest_sources.update({".".join(leaf): source for leaf in leaves(moved)})
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
