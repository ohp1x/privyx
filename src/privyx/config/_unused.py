"""Settings no section uses: unknown keys, and the options of a type not in use."""

from __future__ import annotations

import difflib
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel

from privyx.config.schema import PLUGGABLE, PluggableConfig
from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.config._layers import _Types


#: The :func:`_unused` warnings given in this process.
_UNUSED_WARNED: set[str] = set()


#: The detector settings that name what to mask, each a set of entities.
_MASKING = ("terms", "patterns", "enable")


def _unused(
    layers: list[tuple[str, dict[str, Any]]],
    merged: dict[str, Any],
    origins: dict[str, str],
    types: _Types,
) -> tuple[list[str], list[str]]:
    """Errors and warnings for the settings of a section its built-in ``type`` does not use.

    A setting written in the layer that picks the type, or with no type picked
    above the defaults, is an error.  One from
    another layer is an option of a type no longer in use, and is dropped
    without a word, as is one from the environment, which a container image or
    a compose file sets for every type it offers (``PRIVYX_VAULT_DSN`` for
    ``sqlite`` beside ``PRIVYX_VAULT=redis``).  The exception is a detector's
    ``terms``, ``patterns``, or ``enable``: each entity of theirs that reaches
    no detector using it is a warning, whether a layer picks another type or
    sets a ``detector`` list.  Only keys, entities, types, and layers are
    named, never values.
    """
    refused: list[str] = []
    found: list[str] = []
    for name, model in PLUGGABLE.items():
        value = merged.get(name)
        for index, item in enumerate(value) if isinstance(value, list) else [(None, value)]:
            if not isinstance(item, dict):
                continue
            kind = item.get("type", model.model_fields["type"].default)
            if not isinstance(kind, str) or not types.builtin(name, kind):
                continue  # a plugin's options are its own
            prefix = f"{name}." if index is None else f"{name}.{index}."
            shown = name if index is None else f"{name}[{index}]"
            owner = origins.get(prefix + "type", "defaults")
            used = types.fields(name, kind)
            for key in item:
                if key in used or key not in types.union(name):
                    continue  # an unknown key is an error already
                written = {
                    source
                    for path, source in origins.items()
                    if path == prefix + key or path.startswith(f"{prefix}{key}.")
                }
                for source in sorted(written - {"defaults", "environment"}):
                    if owner in ("defaults", source):
                        refused.append(
                            f"{shown + '.' + key!r} from {source} is not used by {name} type "
                            f"{kind!r}; remove it or set a type that uses it"
                        )
    detector = merged.get("detector")
    final = detector if isinstance(detector, list) else [detector]
    # Per key: an entity's terms do not reach a detector that only has a
    # pattern or an opt-in built-in of that name.
    reached: set[tuple[str, str]] = set()
    for item in final:
        if not isinstance(item, dict) or not isinstance(kind := item.get("type", "regex"), str):
            continue  # a malformed type is pydantic's to report
        plugin = not types.builtin("detector", kind)
        for key in _MASKING:
            if plugin or key in types.fields("detector", kind):
                reached |= {(key, e) for e in _entities(item.get(key), empty=True)}
    if isinstance(detector, list):
        why = f"the detector list from {origins.get('detector')} replaces it"
    else:
        kind = detector.get("type") if isinstance(detector, dict) else None
        why = f"detector type {kind!r} from {origins.get('detector.type')} does not use it"
    for source, data in layers:
        given = data.get("detector") if source != "defaults" else None
        for item in given if isinstance(given, list) else [given]:
            if not isinstance(item, dict):
                continue
            for key in _MASKING:
                for entity in sorted(
                    e for e in _entities(item.get(key)) if (key, e) not in reached
                ):
                    found.append(
                        f"{entity!r} in 'detector.{key}' from {source} reaches no detector "
                        f"that uses it, so it is not masked: {why}"
                    )
    return list(dict.fromkeys(refused)), list(dict.fromkeys(found))


def _entities(value: Any, *, empty: bool = False) -> set[str]:
    """The entities a ``terms``, ``patterns``, or ``enable`` value names.

    An entity given no value, which a layer above may write to drop the terms
    below on purpose, counts only with ``empty``.
    """
    if isinstance(value, dict):
        return {str(entity) for entity, item in value.items() if item or empty}
    if isinstance(value, list):
        return {str(entity) for entity in value if isinstance(entity, str)}
    return set()


def _raise_unknown(unknown: list[str]) -> None:
    if unknown:
        plural = "s" if len(unknown) > 1 else ""
        raise ConfigError(f"unknown setting{plural} {', '.join(unknown)}")


def _unknown_keys(model: type[BaseModel], data: Any, types: _Types, path: str = "") -> list[str]:
    """Name each key in ``data`` that ``model`` does not define, with a suggestion.

    Nested sections and ``detector`` lists are walked too.  A section whose
    ``type`` is built in takes the keys some built-in type of it takes; the
    ones its type does not are :func:`_unused`'s.  One whose ``type`` is not
    built in is skipped: its other keys are the plugin's options.  Only key
    paths are reported, never values — a typo'd key may hold a secret.
    """
    if not isinstance(data, dict):
        return []  # the wrong shape is pydantic's to report
    fields = model.model_fields
    known: Collection[str] = fields
    if issubclass(model, PluggableConfig):
        section = _SECTIONS[model]
        kind = data.get("type", fields["type"].default)
        if not (isinstance(kind, str) and types.builtin(section, kind)):
            return []
        known = types.union(section)
    unknown = []
    for key, value in data.items():
        name = str(key)
        if name not in known:
            match = difflib.get_close_matches(name, list(known), n=1)
            hint = f" (did you mean {match[0]!r}?)" if match else ""
            unknown.append(f"{path + name!r}{hint}")
            continue
        section_model = _section(fields[name].annotation) if name in fields else None
        if section_model is None:
            continue  # a value, or a type's option, which its Options check
        if get_origin(fields[name].annotation) is dict and isinstance(value, dict):
            for entry, item in value.items():
                unknown += _unknown_keys(section_model, item, types, f"{path}{name}.{entry}.")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                unknown += _unknown_keys(section_model, item, types, f"{path}{name}[{i}].")
        else:
            unknown += _unknown_keys(section_model, value, types, f"{path}{name}.")
    return unknown


#: The section of each model in :data:`PLUGGABLE`.
_SECTIONS: dict[type[BaseModel], str] = {model: name for name, model in PLUGGABLE.items()}


def _section(annotation: Any) -> type[BaseModel] | None:
    """The settings model in a field's annotation (``X``, ``X | bool``, ``list[X]``)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _section(arg)
        if found is not None:
            return found
    return None
