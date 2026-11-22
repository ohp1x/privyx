"""The settings renamed in 0.3.0, read the old way with a warning until 0.4.0.

One table: :func:`translate` writes a layer's old keys the new way before
the layers are merged, so the code past the loader sees the new ones only,
and ``privyx config migrate`` writes a file with them (:func:`migrate`).
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from privyx.core import ConfigError

#: Top-level keys that moved into a section of their own.
_MOVED: dict[str, tuple[str, str]] = {
    "host": ("server", "host"),
    "port": ("server", "port"),
    "tls": ("server", "tls"),
    "log_level": ("log", "level"),
    "logging": ("log", "format"),
    "log_file": ("log", "file"),
}

#: The keys of the sections that are gone, or that moved out of ``proxy``: the
#: dotted path each one has now.
_SECTIONS: dict[str, dict[str, str]] = {
    "audit": {
        "enabled": "features.audit.enabled",
        "path": "features.audit.path",
        "prices": "features.audit.prices",
        "tap": "proxy.tap",
        "tap_dir": "proxy.tap_dir",
    },
    "tracing": {"enabled": "features.tracing.enabled"},
    "proxy": {
        "directive": "features.directive.enabled",
        "cache_control": "features.cache_control.enabled",
    },
}

#: The sections of named entries that ``true`` or ``false`` switches.
_SWITCHED = ("detectors", "features")


def translate(
    layer: Mapping[str, Any],
    below: Mapping[str, Any],
    *,
    prefix: str = "",
    deprecated: list[str] | None = None,
) -> dict[str, Any]:
    """``layer``, its presets, and its profiles' ``privyx`` settings, with the old keys moved.

    ``below`` holds the ``detectors`` of the layers below: what an old
    ``detector`` changes (:func:`_detectors`).  An entry of ``detectors`` or
    ``features`` written ``true`` or ``false`` becomes ``enabled: true`` or
    ``false``, so a later layer keeps the options of an earlier one.  Each
    old key read adds a warning to ``deprecated``, naming keys, never values.

    Raises:
        ConfigError: If a section that is gone holds a key it never took.
    """
    warnings: list[str] = [] if deprecated is None else deprecated
    data = _translated(dict(layer), below, prefix, warnings)
    # A preset or a profile applies over this layer's own detectors.
    detectors = _merge(dict(below), data.get("detectors"))
    presets = data.get("presets")
    if isinstance(presets, dict):
        data["presets"] = {
            name: _translated(dict(p), detectors, f"{prefix}presets.{name}.", warnings)
            if isinstance(p, dict)
            else p
            for name, p in presets.items()
        }
    agents = data.get("agents")
    if isinstance(agents, dict) and any(
        isinstance(p, dict) and isinstance(p.get("privyx"), dict) for p in agents.values()
    ):
        data["agents"] = {
            name: {
                **profile,
                "privyx": _translated(
                    dict(profile["privyx"]), detectors, f"{prefix}agents.{name}.privyx.", warnings
                ),
            }
            if isinstance(profile, dict) and isinstance(profile.get("privyx"), dict)
            else profile
            for name, profile in agents.items()
        }
    return data


def _translated(
    data: dict[str, Any], below: Mapping[str, Any], prefix: str, warnings: list[str]
) -> dict[str, Any]:
    """``data``, one layer's settings or a patch of them, with the old keys moved.

    A new key takes the place of the old one, and one written beside it in
    the same layer wins over it.
    """

    def moved(old: str, new: str) -> None:
        warnings.append(
            f"{prefix + old!r} is deprecated; write {prefix + new!r} (removed in 0.4.0)"
        )

    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _MOVED:
            section, name = _MOVED[key]
            moved(key, f"{section}.{name}")
            _put(out, (section, name), value)
        elif key in _SECTIONS and isinstance(value, dict) and _SECTIONS[key].keys() & value.keys():
            keys = _SECTIONS[key]
            rest = {k: v for k, v in value.items() if k not in keys}
            for old, path in keys.items():
                if old in value:
                    moved(f"{key}.{old}", path.removesuffix(".enabled"))
                    _put(out, path.split("."), value[old])
            if key == "proxy":
                if rest:
                    out[key] = _merge(out.get(key), rest)
            elif rest:
                name = next(iter(rest))
                match = difflib.get_close_matches(name, list(keys), n=1)
                hint = f" (did you mean {key + '.' + match[0]!r}?)" if match else ""
                raise ConfigError(f"unknown setting {prefix + key + '.' + name!r}{hint}")
        elif key == "detector" and (entries := _detectors(value, below)) is not None:
            yaml = (
                value.get("type") == "yaml"
                if isinstance(value, dict)
                else any(_kind(item) == "yaml" for item in value)
            )
            names = ", ".join(repr(f"{prefix}detectors.{name}") for name in entries)
            warnings.append(
                f"{prefix + 'detector'!r} is deprecated; write {names}"
                + (" with 'builtins: false' for type 'yaml'" if yaml else "")
                + " (removed in 0.4.0)"
            )
            # The new key, written in the same layer, wins.
            out["detectors"] = _merge(entries, out.get("detectors"))
        else:
            out[key] = _merge(out.get(key), value) if key in out else value
    for section in _SWITCHED:
        entries = out.get(section)
        if isinstance(entries, dict):
            out[section] = {
                name: {"enabled": entry}
                if isinstance(entry, bool)
                else ({} if entry is None else entry)
                for name, entry in entries.items()
            }
    written = out.get("detectors")
    if isinstance(written, dict):
        for name, entry in written.items():
            if isinstance(entry, dict) and entry.get("type") == "yaml":
                warnings.append(
                    f"{prefix}detectors.{name}.type 'yaml' is deprecated; write 'type: regex' "
                    "with 'builtins: false' (removed in 0.4.0)"
                )
                written[name] = {**entry, "type": "regex", "builtins": False}
    return out


def _merge(below: Any, above: Any) -> Any:
    """``above`` merged over ``below``, mapping by mapping; ``above`` wins."""
    if not isinstance(below, dict) or not isinstance(above, dict):
        return above if above is not None else below
    merged = dict(below)
    for key, value in above.items():
        merged[key] = _merge(merged.get(key), value) if key in merged else value
    return merged


def _put(data: dict[str, Any], path: Sequence[str], value: Any) -> None:
    """Set ``value`` at ``path`` in ``data``, unless the layer wrote the new key there too.

    The mappings on the way are copied: the layer's own are left as they are.
    """
    for key in path[:-1]:
        nested = data.get(key, {})
        if not isinstance(nested, dict):
            return  # the new key, written another way, is the schema's to report
        data[key] = nested = dict(nested)
        data = nested
    data.setdefault(path[-1], value)


def _kind(item: Any) -> str:
    """The detector type an old ``detector`` mapping names; ``regex`` without one."""
    kind = item.get("type") if isinstance(item, dict) else None
    return kind if isinstance(kind, str) and kind else "regex"


def _entry(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The type of an old ``detector`` mapping, and its settings as a ``detectors`` entry.

    Type ``yaml`` is ``regex`` without its built-in patterns.  ``regex`` is
    written with them, since a layer below may have left them off with
    ``yaml``, and naming ``regex`` over it turned them on.
    """
    entry = dict(item)
    kind = _kind(entry)
    if kind == "yaml":
        kind = "regex"
        entry["builtins"] = False
    elif kind == "regex":
        entry.setdefault("builtins", True)
    entry["type"] = kind
    return kind, entry


def _on(entry: Any) -> bool:
    return not (isinstance(entry, dict) and entry.get("enabled") is False)


def _detectors(old: Any, below: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``detectors`` entries that do what the old ``detector`` value did over ``below``.

    A mapping sets each detector that is on, as it did each item of a list;
    with a ``type``, each takes that type and keeps what was set for it.
    The one default detector, with nothing set, gives way to an entry named
    like the new type.  A list replaces them all, what they were set to
    included: an item takes a name of its own when the entry of its type
    holds settings from below, which would otherwise be merged into it (a
    ``disable`` that turns a built-in off), and that entry is turned off.
    ``None`` for a value of the wrong shape.  The types are written out, so
    the layer counts as the one that picked them.
    """
    on = [str(name) for name, entry in below.items() if _on(entry)] or ["regex"]
    if isinstance(old, dict) and not old.get("type"):
        return {name: dict(old) for name in on}
    if isinstance(old, dict):
        kind, entry = _entry(old)
        if len(on) > 1 or _holds_settings(below.get(on[0])):
            return {name: dict(entry) for name in on}
        name = _free(kind, below, ())
        if not _on(below.get(name)):
            entry["enabled"] = True
        return {**({on[0]: {"enabled": False}} if on[0] != name else {}), name: entry}
    if not isinstance(old, list) or not all(isinstance(item, dict) for item in old):
        return None
    entries: dict[str, Any] = {}
    for item in old:
        kind, entry = _entry(item)
        name = _free(kind, below, entries)
        if not _on(below.get(name)):
            entry["enabled"] = True
        entries[name] = entry
    return {**{name: {"enabled": False} for name in on if name not in entries}, **entries}


def _free(kind: str, below: Mapping[str, Any], taken: Collection[str]) -> str:
    """A name for a new detector of type ``kind``: its own, unless that one is in use."""
    name, n = kind, 1
    while name in taken or _holds_settings(below.get(name)):
        n += 1
        name = f"{kind}-{n}"
    return name


def _holds_settings(entry: Any) -> bool:
    """Whether a ``detectors`` entry holds more than its type and its switch."""
    return isinstance(entry, dict) and bool(entry.keys() - {"type", "enabled"})


def detectors_of(layers: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``detectors`` of config ``layers``, lowest first, over the default one."""
    below: dict[str, Any] = {"regex": {}}
    for layer in layers:
        below = _merge(below, translate(layer, below).get("detectors"))
    return below


def migrate(
    data: Mapping[str, Any],
    warnings: list[str] | None = None,
    *,
    below: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A config file's settings written the 0.3.0 way, for ``privyx config migrate``.

    The keys renamed for the provider, the operator, and the agent profiles
    are moved too.  An entry switched with ``true`` or ``false`` is written
    so again, and a detector named like its type without ``type``.  ``below``
    holds the ``detectors`` of the files read below this one
    (:func:`detectors_of`): an old ``detector`` changed what they set.
    """
    from privyx.config.agents import translate_adapters
    from privyx.config.providers import translate_keys, translate_operator

    found: list[str] = [] if warnings is None else warnings
    layer = translate_adapters(dict(data), found)
    layer = translate(layer, {"regex": {}} if below is None else below, deprecated=found)
    layer = translate_keys(layer, "file", found)
    operator = layer.get("operator")
    kind = operator.get("type") if isinstance(operator, dict) else None
    layer = translate_operator(layer, kind if isinstance(kind, str) else "pseudonym", "file", found)
    if "upstream_url" in layer:
        url = layer.pop("upstream_url")
        provider = layer.get("provider")
        if isinstance(provider, str):
            named = layer.setdefault("providers", {}).setdefault(provider, {})
            named.setdefault("base_url", url)
        elif isinstance(provider, dict) or provider is None:
            layer["provider"] = {**(provider or {}), "base_url": url}
    return _tidy(layer)


def _tidy(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` with each switched entry as short as it can be written."""
    for section in _SWITCHED:
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        tidy: dict[str, Any] = {}
        for name, entry in entries.items():
            if isinstance(entry, dict):
                entry = {k: v for k, v in entry.items() if not (k == "type" and v == name)}
                if set(entry) == {"enabled"} and isinstance(entry["enabled"], bool):
                    entry = entry["enabled"]
            tidy[name] = entry
        data[section] = tidy
    for name in ("presets",):
        nested = data.get(name)
        if isinstance(nested, dict):
            data[name] = {k: _tidy(v) if isinstance(v, dict) else v for k, v in nested.items()}
    agents = data.get("agents")
    if isinstance(agents, dict):
        for profile in agents.values():
            if isinstance(profile, dict) and isinstance(profile.get("privyx"), dict):
                profile["privyx"] = _tidy(profile["privyx"])
    return data
