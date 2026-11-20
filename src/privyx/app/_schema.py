"""The JSON Schema of a config file, from the settings and the component registries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from privyx.config.loader import _NOT_IN_PRESET
from privyx.config.schema import PLUGGABLE, PluggableConfig, Settings

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.core import Registry

#: Where the published copy of :func:`json_schema` is served (``docs/schema.json``).
SCHEMA_URL = "https://ohp1x.github.io/privyx/schema.json"


def json_schema(app: Privyx | None = None) -> dict[str, Any]:
    """The JSON Schema of a config file, for editors that complete and check YAML.

    A section without a ``type`` takes no other keys, as in
    :func:`~privyx.config.loader.load_config`.  One with a ``type`` takes,
    under a built-in type, only that type's options and the shared ones, and
    any key under another.

    With ``app``, whose plugins are loaded, the types its plugins added check
    their options too, and ``type`` takes only the names the app knows.
    Without plugins it is the published schema.
    """
    from privyx.app._privyx import Privyx

    registries = (app or Privyx())._registries
    closed = app is not None and any(
        not registries[name].is_builtin(kind)
        for name in PLUGGABLE
        for kind in registries[name].names()
    )
    schema = Settings.model_json_schema()
    schema["properties"]["include"] = {
        "description": "Config files or globs to merge below this one, relative to it.",
        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
    }
    for section in [schema, *schema["$defs"].values()]:
        section.setdefault("additionalProperties", False)
    # A profile may take its command from the profile it extends.
    schema["$defs"]["AgentConfig"].pop("required", None)
    _allow_references(schema)
    for name, model in PLUGGABLE.items():
        _by_type(schema["$defs"], name, model, registries[name], closed=closed)
    # A profile's settings and a preset are partial config files, less the
    # keys the loader refuses there; `include` is a file's own.
    for name, refused in (
        ("ProfileSettings", ("agents", "presets", "include")),
        ("PresetSettings", _NOT_IN_PRESET),
    ):
        kept = {k: v for k, v in schema["properties"].items() if k not in refused}
        schema["$defs"][name] = {"type": "object", "properties": kept}
        schema["$defs"][name]["additionalProperties"] = False
    profile = schema["$defs"]["AgentConfig"]["properties"]
    profile["privyx"] = {"title": "Privyx", "$ref": "#/$defs/ProfileSettings"}
    schema["properties"]["presets"]["additionalProperties"] = {"$ref": "#/$defs/PresetSettings"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_URL, **schema}


def _by_type(
    defs: dict[str, Any],
    section: str,
    model: type[PluggableConfig],
    registry: Registry[Any],
    *,
    closed: bool,
) -> None:
    """Make ``model``'s definition check each ``type`` with that type's options.

    The checks are ``if``/``then`` pairs, so a ``${NAME}`` type, which matches
    no name, is checked as an unknown plugin's: with any key.  Built-in types
    whose options are the same share one check.  ``closed``: ``type`` takes
    only the registered names and references, not any other string.
    """
    node = defs[model.__name__]
    builtin = registry.builtin_names()
    plugins = sorted(name for name in registry.names() if not registry.is_builtin(name))
    names = sorted(builtin) + (plugins if closed else [])
    kind = {k: v for k, v in node["properties"]["type"].items() if k in ("default", "title")}
    if closed:
        kind["anyOf"] = [{"enum": names}, {"type": "string", "pattern": r"\$\{"}]
    else:  # suggest the built-in names, take any other
        kind["anyOf"] = [{"enum": names}, {"type": "string"}]
    props = node["properties"]
    shared = model.shared_fields()
    # Only `type` and the shared fields apply whatever the type; each type adds
    # its own below, so an editor offers only those.
    node["properties"] = {"type": kind, **{k: props[k] for k in props if k in shared}}
    node["additionalProperties"] = True
    taken = {"type": True, **dict.fromkeys(shared, True)}
    default = model.model_fields["type"].default
    groups: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for name in builtin:
        own = _own(registry.get(name).Options.model_json_schema(), shared)
        groups.setdefault(json.dumps(own, sort_keys=True), (own, []))[1].append(name)
    checks: list[dict[str, Any]] = []
    for own, types in groups.values():
        check: dict[str, Any] = {"properties": {"type": {"enum": types}}}
        if default not in types:  # the default's check also applies with no `type`
            check["required"] = ["type"]
        checks.append(
            {"if": check, "then": {"properties": {**taken, **own}, "additionalProperties": False}}
        )
    for name in plugins if closed else []:
        prefix = f"{section}.{name}."
        options = registry.get(name).Options
        mine = options.model_json_schema(ref_template=f"#/$defs/{prefix}{{model}}")
        for key, value in mine.pop("$defs", {}).items():
            defs[prefix + key] = value
        checks.append(
            {
                "if": {"properties": {"type": {"const": name}}, "required": ["type"]},
                "then": {
                    "properties": {**taken, **_own(mine, shared)},
                    "additionalProperties": False,
                },
            }
        )
    node["allOf"] = checks


def _own(schema: dict[str, Any], shared: list[str]) -> dict[str, Any]:
    """The properties of an ``Options`` schema that are a type's own, not the shared ones."""
    own = {k: v for k, v in schema.get("properties", {}).items() if k not in shared}
    _allow_references(own)
    return own


def _allow_references(node: Any) -> None:
    """Let a number, boolean, or enum be a ``${NAME}`` reference, which the loader resolves."""
    if isinstance(node, list):
        for item in node:
            _allow_references(item)
    elif isinstance(node, dict):
        restricted = "enum" in node or "const" in node
        if restricted or node.get("type") in ("integer", "number", "boolean"):
            value = {k: node.pop(k) for k in list(node) if k not in ("title", "description")}
            node["anyOf"] = [value, {"type": "string", "pattern": r"\$\{"}]
        else:
            for item in node.values():
                _allow_references(item)
