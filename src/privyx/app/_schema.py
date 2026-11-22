"""The JSON Schema of a config file, from the settings and the component registries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from privyx.config._presets import _NOT_IN_PRESET
from privyx.config.schema import NAMED, PLUGGABLE, AgentConfig, PluggableConfig, Settings

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.core import Registry

#: Where the published copy of :func:`json_schema` is served (``docs/schema.json``).
SCHEMA_URL = "https://ohp1x.github.io/privyx/schema.json"

_LENGTH = {"type": "integer", "minimum": 6, "maximum": 64}

#: The settings renamed in 0.3.0, still read with a warning until 0.4.0: an
#: editor shows them struck through.  By section, then by type (``*``: any).
_DEPRECATED_OPTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "provider": {
        "*": {
            f"{name}_api_key": {"type": "string", "description": f"Write providers.{name}.api_key."}
            for name in ("openai", "anthropic", "google")
        }
    },
    "operator": {
        "hash": {"length": {**_LENGTH, "description": "Write id_length."}},
        "redact": {"token": {"type": "string", "description": "Write text."}},
    },
}


def json_schema(app: Privyx | None = None) -> dict[str, Any]:
    """The JSON Schema of a config file, for editors that complete and check YAML.

    A section without a ``type`` takes no other keys, as in
    :func:`~privyx.config.load_config`.  One with a ``type`` takes,
    under a built-in type, only that type's options and the shared ones, and
    any key under another.

    With ``app``, whose plugins are loaded, the types its plugins added check
    their options too, and ``type`` takes only the names the app knows.
    Without plugins it is the published schema.
    """
    from privyx.app._privyx import Privyx

    registries = (app or Privyx())._registries
    kinds = (*PLUGGABLE, *(kind for kind, _ in NAMED.values()), "agent")
    closed = app is not None and any(
        not registries[name].is_builtin(kind) for name in kinds for kind in registries[name].names()
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
    _deprecated(schema)
    for name, model in PLUGGABLE.items():
        default = model.model_fields["type"].default
        _by_type(schema["$defs"], name, model, registries[name], closed=closed, default=default)
    for name, (kind, model) in NAMED.items():
        _by_type(schema["$defs"], name, model, registries[kind], closed=closed, default=None)
        _named(schema, name, model)
    # `provider` names one of `providers`, or is a definition of its own.
    schema["properties"]["provider"] = {
        "title": "Provider",
        "anyOf": [
            {"type": "string", "description": "The name of one of providers."},
            {"$ref": "#/$defs/ProviderConfig"},
        ],
    }
    _by_type(schema["$defs"], "agents", AgentConfig, registries["agent"], closed=closed, default="")
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
    model: type[BaseModel],
    registry: Registry[Any],
    *,
    closed: bool,
    default: str | None,
) -> None:
    """Make ``model``'s definition check each ``type`` with that type's options.

    The checks are ``if``/``then`` pairs, so a ``${NAME}`` type, which matches
    no name, is checked as an unknown plugin's: with any key.  Built-in types
    whose options are the same share one check.  ``closed``: ``type`` takes
    only the registered names and references, not any other string.
    ``default`` is the type without a ``type`` key; ``None`` for an entry of
    a mapping, whose type is its name's (:func:`_named`).
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
    # An agent profile's own fields apply whatever its type, as a section's shared ones do.
    shared = (
        model.shared_fields()
        if issubclass(model, PluggableConfig)
        else [name for name in model.model_fields if name != "type"]
    )
    # Only `type` and the shared fields apply whatever the type; each type adds
    # its own below, so an editor offers only those.
    node["properties"] = {"type": kind, **{k: props[k] for k in props if k in shared}}
    node["additionalProperties"] = True
    taken = {"type": True, **dict.fromkeys(shared, True)}
    groups: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for name in builtin:
        options = registry.get(name).Options.model_json_schema()
        for key, value in options.pop("$defs", {}).items():
            defs.setdefault(key, value)  # the models of its options, such as ModelPrice
        own = _own(options, shared)
        groups.setdefault(json.dumps(own, sort_keys=True), (own, []))[1].append(name)
    checks: list[dict[str, Any]] = []
    for own, types in groups.values():
        check: dict[str, Any] = {"properties": {"type": {"enum": types}}}
        if default is None or default not in types:  # the default's applies with no `type`
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
    for owner, options in _DEPRECATED_OPTIONS.get(section, {}).items():
        marked = {key: {**value, "deprecated": True} for key, value in options.items()}
        _allow_references(marked)
        if owner == "*":
            node["properties"].update(marked)
        for check in checks:
            if owner == "*" or owner in check["if"]["properties"]["type"].get("enum", ()):
                check["then"]["properties"].update(marked)
    node["allOf"] = checks


def _named(schema: dict[str, Any], section: str, model: type[BaseModel]) -> None:
    """Make ``section`` a mapping of entries whose type is their name's unless they write one.

    Each type's check applies to the entry named like it with no ``type``;
    ``true`` or ``false`` switches an entry.  The checks and the section are
    definitions of their own, so the settings of a profile and of a preset
    refer to them rather than copy them.
    """
    defs = schema["$defs"]
    ref = {"$ref": f"#/$defs/{model.__name__}"}
    switch = {"type": "boolean"}
    entries: dict[str, Any] = {}
    for check in defs[model.__name__]["allOf"]:
        kind = check["if"]["properties"]["type"]
        names = kind.get("enum", [kind.get("const")])
        options = f"{model.__name__}.{names[0]}"
        defs[options] = check["then"]
        check["then"] = {"$ref": f"#/$defs/{options}"}
        for name in names:
            implied = {"if": {"not": {"required": ["type"]}}, "then": check["then"]}
            entries[name] = {"anyOf": [switch, {"allOf": [ref, implied]}]}
    field = schema["properties"][section]
    name = section.title()
    defs[name] = {
        "title": field.get("title", name),
        "type": "object",
        "properties": entries,
        "additionalProperties": {"anyOf": [switch, ref]},
    }
    schema["properties"][section] = {"$ref": f"#/$defs/{name}"}


def _deprecated(schema: dict[str, Any]) -> None:
    """Add the keys renamed in 0.3.0, read with a warning until 0.4.0, as deprecated."""
    props = schema["properties"]
    defs = schema["$defs"]
    upstream = props["upstream_url"]
    upstream.update(deprecated=True, description="Write provider.base_url.")
    proxy = defs["ProxyConfig"]["properties"]
    for key in (
        "forward_client_auth",
        "path_mapping",
        "timeout",
        "connect_timeout",
        "max_connections",
    ):
        moved = defs["ProviderConfig"]["properties"][key]
        proxy[key] = {**moved, "deprecated": True, "description": f"Write provider.{key}."}
    for key in ("directive", "cache_control"):
        proxy[key] = {
            "type": "boolean",
            "deprecated": True,
            "description": f"Write features.{key}.",
        }
    token = defs["TokenConfig"]["properties"]
    token["id_length"] = {**_LENGTH, "deprecated": True, "description": "Write operator.id_length."}
    _allow_references(token["id_length"])
    server, log = defs["ServerConfig"]["properties"], defs["LogConfig"]["properties"]
    for old, new, moved in (
        ("host", "server.host", server["host"]),
        ("port", "server.port", server["port"]),
        ("tls", "server.tls", server["tls"]),
        ("log_level", "log.level", log["level"]),
        ("logging", "log.format", log["format"]),
        ("log_file", "log.file", log["file"]),
    ):
        props[old] = {**moved, "deprecated": True, "description": f"Write {new}."}
    entry = {"type": "object", "properties": {"type": {"type": "string"}}}
    props["detector"] = {
        "anyOf": [entry, {"type": "array", "items": entry}],
        "deprecated": True,
        "description": "Write detectors, with an entry per detector.",
    }
    props["audit"] = {
        "type": "object",
        "deprecated": True,
        "description": "Write features.audit, and proxy.tap and proxy.tap_dir.",
        "properties": {
            "enabled": {"type": "boolean"},
            "path": {"type": "string"},
            "tap": {"type": "boolean"},
            "tap_dir": {"type": "string"},
            "prices": {"type": "object", "additionalProperties": {"$ref": "#/$defs/ModelPrice"}},
        },
        "additionalProperties": False,
    }
    props["tracing"] = {
        "type": "object",
        "deprecated": True,
        "description": "Write features.tracing.",
        "properties": {"enabled": {"type": "boolean"}},
        "additionalProperties": False,
    }
    for name in ("detector", "audit", "tracing"):
        _allow_references(props[name])


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
