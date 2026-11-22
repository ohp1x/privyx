"""Setting keys: the dotted path of a setting, and the type of its value.

A key is resolved against the settings model and, where a section names a
component, the options of that component's type: ``vault.ttl`` exists only
while the vault is a type that takes ``ttl``.  The environment and
``privyx config set`` give values as text, which :func:`parse` reads by the
type of the setting.
"""

from __future__ import annotations

import difflib
import types as builtin_types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from privyx.config.schema import NAMED, PLUGGABLE, AgentConfig, Settings

if TYPE_CHECKING:
    from privyx.config._layers import _Types

#: The annotation of a key whose type is not known: a plugin's option.
UNKNOWN: Any = object()


@dataclass(frozen=True)
class Key:
    """A setting's dotted path, as the settings spell it, and the type of its value."""

    path: tuple[str, ...]
    annotation: Any

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


def resolve(
    parts: Sequence[str], merged: Mapping[str, Any], types: _Types, *, existing: bool
) -> Key | str:
    """The setting ``parts`` names, or why it names none.

    With ``existing`` (the environment), a part matches a name whatever its
    case, and an entry of a mapping (a detector, a profile, an entity) only
    if ``merged`` has it already.  Without, the parts are taken as written,
    and a new entry is a key too.
    """
    annotation: Any = Settings
    data: Any = merged
    kind: tuple[str, str] | None = None  # (kind, default type) of a component's settings
    path: list[str] = []
    for part in parts:
        model = _model(annotation)
        if annotation is UNKNOWN or annotation is Any:
            name = part.lower() if existing else part
            path.append(name)
            data = data.get(name) if isinstance(data, dict) else None
            continue
        if model is not None:
            fields: dict[str, Any] = {k: f.annotation for k, f in model.model_fields.items()}
            if kind is not None:
                options = _options(kind, data, types)
                if options is None:
                    fields = {**fields, part.lower() if existing else part: UNKNOWN}
                else:
                    fields = {**options, **fields}
            field = _match(part, fields, existing=existing)
            if field is None:
                return _unknown(path, part, fields)
            annotation, kind = fields[field], _kind_of(field, model)
            path.append(field)
            data = data.get(field) if isinstance(data, dict) else None
            continue
        if get_origin(annotation) is dict:
            entries = data if isinstance(data, dict) else {}
            entry = _match(part, entries, existing=True) if existing else part
            if entry is None:
                where = ".".join(path)
                return (
                    f"{where!r} has no entry {part.lower()!r}; add it in a config file "
                    "or with `privyx config set`"
                )
            value = get_args(annotation)[1] if get_args(annotation) else Any
            kind = _entry_kind(path, entry, entries.get(entry), value)
            annotation = value
            path.append(entry)
            data = entries.get(entry)
            continue
        return f"{'.'.join(path)!r} is a value, with no key {part.lower()!r} under it"
    return Key(tuple(path), annotation)


def _model(annotation: Any) -> type[BaseModel] | None:
    """The settings model of a section, also inside ``Model | bool``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, builtin_types.UnionType):
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _match(part: str, names: Mapping[str, Any], *, existing: bool) -> str | None:
    if part in names:
        return part
    if existing:
        folded = {str(name).lower(): str(name) for name in names}
        return folded.get(part.lower())
    return None


def _unknown(path: Sequence[str], part: str, fields: Mapping[str, Any]) -> str:
    where = ".".join([*path, part.lower()])
    match = difflib.get_close_matches(part.lower(), list(fields), n=1)
    hint = f" (did you mean {'.'.join([*path, match[0]])!r}?)" if match else ""
    return f"no setting {where!r}{hint}"


def _kind_of(name: str, model: type[BaseModel]) -> tuple[str, str] | None:
    """The kind and default type of the section at ``name`` of ``model``, if it names one."""
    if model is Settings and name in PLUGGABLE:
        return name, PLUGGABLE[name].model_fields["type"].default
    return None


def _entry_kind(path: Sequence[str], name: str, entry: Any, value: Any) -> tuple[str, str] | None:
    """The kind and default type of entry ``name`` of the mapping at ``path``."""
    section = ".".join(path)
    if section in NAMED:
        return NAMED[section][0], name
    if section == "providers":
        return "provider", "generic"
    if section == "agents" and value is AgentConfig:
        kind = entry.get("type") if isinstance(entry, dict) else None
        return "agent", kind if isinstance(kind, str) and kind else name
    return None


def _options(kind: tuple[str, str], data: Any, types: _Types) -> dict[str, Any] | None:
    """The options of the type ``data`` names, each with its annotation.

    ``None`` when the type is not registered, as a plugin's is before the
    plugins a config names are set up: then any key is one of its options.
    """
    name = data.get("type", kind[1]) if isinstance(data, dict) else kind[1]
    registry = types.registry(kind[0])
    if not isinstance(name, str) or registry is None or name not in registry:
        return None
    options = registry.get(name).Options
    return {key: field.annotation for key, field in options.model_fields.items()}


def parse(raw: str, annotation: Any) -> Any:
    """A value given as text, read by the type of its setting.

    A list, a mapping, or a section is read as YAML flow (``[a, b]``,
    ``{a: 1}``); anything else is left as text for the settings model to
    convert, so ``NO`` stays a string where a string is wanted.
    """
    if annotation is UNKNOWN:
        return raw
    if _container(annotation):
        try:
            return yaml.safe_load(raw)
        except yaml.YAMLError:
            return raw  # reported as the wrong type by the settings model
    return raw


def typed(raw: str, annotation: Any, key: str) -> Any:
    """A value given as text, as the type of its setting: ``8001`` for a port is a number.

    A list, a mapping, or a section is read as YAML flow, as :func:`parse`
    does; a value of a type not known (a plugin's option) is left as text.

    Raises:
        ConfigError: If the text is not a value of that type; the message
            names the key, never the value.
    """
    from pydantic import TypeAdapter, ValidationError

    from privyx.core import ConfigError

    value = parse(raw, annotation)
    if annotation is UNKNOWN or annotation is Any or _container(annotation):
        return value
    try:
        return TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        reason = exc.errors(include_input=False)[0]["msg"]
        raise ConfigError(f"invalid value for {key!r}: {reason}") from None


def _container(annotation: Any) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in (list, dict, set, tuple):
        return True
    if origin in (Union, builtin_types.UnionType):
        return any(_container(arg) for arg in get_args(annotation) if arg is not type(None))
    return isinstance(annotation, type) and issubclass(annotation, (BaseModel, list, dict))


def env_name(key: Sequence[str]) -> str | None:
    """The generic environment variable of ``key``, if its names can be written in one."""
    if all(part.replace("_", "a").isalnum() and part.isascii() for part in key):
        return "PRIVYX_" + "__".join(part.upper() for part in key)
    return None


def leaves(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """The paths of the values in ``data``."""
    found: list[tuple[str, ...]] = []
    for key, value in data.items():
        if isinstance(value, dict) and value:
            found += leaves(value, (*prefix, str(key)))
        else:
            found.append((*prefix, str(key)))
    return found
