"""The settings the environment sets: ``PRIVYX_<PATH>``, and the older names kept as aliases.

Every setting has a variable: ``PRIVYX_`` and its path with ``__`` between
the names, such as ``PRIVYX_SERVER__PORT`` or
``PRIVYX_DETECTORS__REGEX__TERMS``.  A name matches whatever its case, but
only a setting, or an entry the config has already (a detector, a profile,
an entity): a new entry is written in a file or with ``privyx config set``.
The variables of earlier versions (:data:`ALIASES`) set the same settings;
given both ways with different values, the config is refused.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from privyx.config._keys import UNKNOWN, parse, resolve
from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.config._layers import _Types

#: The variables of earlier versions, each with the setting it sets.  The
#: ``TLS_`` names are the ``SSL_`` ones spelled another way.
ALIASES: dict[str, str] = {
    "PRIVYX_HOST": "server.host",
    "PRIVYX_PORT": "server.port",
    "PRIVYX_SSL_CERTFILE": "server.tls.certfile",
    "PRIVYX_SSL_KEYFILE": "server.tls.keyfile",
    "PRIVYX_SSL_KEYFILE_PASSWORD": "server.tls.keyfile_password",
    "PRIVYX_SSL_CA_CERTS": "server.tls.ca_certs",
    "PRIVYX_TLS_CERTFILE": "server.tls.certfile",
    "PRIVYX_TLS_KEYFILE": "server.tls.keyfile",
    "PRIVYX_TLS_KEYFILE_PASSWORD": "server.tls.keyfile_password",
    "PRIVYX_TLS_CA_CERTS": "server.tls.ca_certs",
    "PRIVYX_LOG_LEVEL": "log.level",
    "PRIVYX_LOGGING": "log.format",
    "PRIVYX_LOG_FILE": "log.file",
    "PRIVYX_PROVIDER": "provider",
    "PRIVYX_API_KEY": "provider.api_key",
    "PRIVYX_UPSTREAM_URL": "provider.base_url",
    "PRIVYX_OPENAI_API_KEY": "providers.openai.api_key",
    "PRIVYX_ANTHROPIC_API_KEY": "providers.anthropic.api_key",
    "PRIVYX_GOOGLE_API_KEY": "providers.google.api_key",
    "PRIVYX_VAULT": "vault.type",
    "PRIVYX_VAULT_DSN": "vault.dsn",
    "PRIVYX_REDIS_URL": "vault.redis_url",
    "PRIVYX_VAULT_TTL": "vault.ttl",
    "PRIVYX_ANCHOR_SECRET": "anchor.secret",
    "PRIVYX_SESSION_STRATEGY": "session.strategy",
    "PRIVYX_ENCRYPT_KEY": "operator.key",
    "PRIVYX_TOKEN_FORMAT": "token.format",
    "PRIVYX_TOKEN_NAMESPACE": "token.namespace",
    "PRIVYX_DETECTOR_CACHE": "detectors.*.cache",
    "PRIVYX_AUDIT_ENABLED": "features.audit.enabled",
    "PRIVYX_AUDIT_PATH": "features.audit.path",
    "PRIVYX_PLUGIN_PATHS": "plugins.paths",
    "PRIVYX_PLUGIN_MODULES": "plugins.modules",
    "PRIVYX_AGENTS": "agents",
}

#: The settings at the top that a name without ``__`` sets: the others are
#: sections, or have an alias of the same name.
_TOP = ("harvest",)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"PRIVYX_{key}", default)


def _env_bool(key: str) -> bool:
    # A typo must not read as false: PRIVYX_AUDIT_ENABLED=treu would turn the
    # audit trail off without a word.
    value = _env(key).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"PRIVYX_{key} must be 1, true, yes, on, 0, false, no, or off")


def _names(value: str) -> list[str]:
    """A comma-separated list, each item stripped, the empty ones dropped."""
    return [name.strip() for name in value.split(",") if name.strip()]


def discovery_disabled() -> bool:
    """Whether ``PRIVYX_NO_DISCOVERY`` turns off reading config files not named."""
    return bool(_env("NO_DISCOVERY")) and _env_bool("NO_DISCOVERY")


def env_config(merged: Mapping[str, Any], types: _Types) -> dict[str, Any]:
    """The settings the environment sets, over ``merged``, the config below it.

    Raises:
        ConfigError: If a ``PRIVYX_<PATH>`` name is no setting, or names one
            an alias gives another value.
    """
    cfg, given = _aliases(merged)
    provider = _env("PROVIDER")
    typed = _with_env_types(merged, given, types)
    for variable, raw in sorted(os.environ.items()):
        if not variable.startswith("PRIVYX_"):
            continue
        name = variable.removeprefix("PRIVYX_")
        if "__" not in name and name.lower() not in _TOP:
            continue  # an alias, or a variable that is not a setting
        found = resolve(name.split("__"), typed, types, existing=True)
        if isinstance(found, str):
            raise ConfigError(f"{variable} names no setting: {found}")
        value = parse(raw, found.annotation)
        other = given.get(found.dotted)
        if other is not None and not _same(other[1], value, found.annotation):
            raise ConfigError(f"{other[0]} and {variable} set {found.dotted!r} to different values")
        given[found.dotted] = (variable, value)
        _put(cfg, _written(list(found.path), provider), value)
    return cfg


def env_operator_types() -> dict[str, Any]:
    """The operator types the environment picks, as presets of a config layer.

    For ``privyx config migrate``: they apply over the files only while they
    are set, so a key one of them reads cannot be judged by the files alone.
    """
    picked = {
        value
        for variable, value in os.environ.items()
        if value
        and variable.startswith("PRIVYX_")
        and f"__{variable.removeprefix('PRIVYX_').upper()}".endswith("__OPERATOR__TYPE")
    }
    layer: dict[str, Any] = {
        "presets": {f"environment-{kind}": {"operator": {"type": kind}} for kind in picked}
    }
    try:
        agents = json.loads(_env("AGENTS") or "{}")
    except json.JSONDecodeError:
        return layer  # reported when the config is read
    if isinstance(agents, dict):
        layer["agents"] = agents
    return layer


def _with_env_types(
    merged: Mapping[str, Any], given: Mapping[str, tuple[str, Any]], types: _Types
) -> dict[str, Any]:
    """``merged`` with the types the environment picks, which decide the options it can set.

    Read before the other variables: sorted, ``__DSN`` comes before ``__TYPE``.
    """

    def sections(data: Mapping[str, Any]) -> dict[str, Any]:
        return {k: sections(v) if isinstance(v, dict) else v for k, v in data.items()}

    typed = sections(merged)
    for dotted, (_, value) in given.items():
        if dotted.endswith(".type"):
            _put(typed, dotted.split("."), value)
    for variable, raw in sorted(os.environ.items()):
        parts = variable.removeprefix("PRIVYX_").split("__")
        if variable.startswith("PRIVYX_") and len(parts) > 1 and parts[-1].lower() == "type":
            found = resolve(parts, typed, types, existing=True)
            if not isinstance(found, str):  # reported with the other variables
                _put(typed, list(found.path), raw)
    return typed


def _written(path: list[str], provider: str) -> list[str]:
    """Where the environment writes the setting at ``path``.

    The provider selected at the top takes ``provider.*``: by name, under
    ``providers``, when ``PRIVYX_PROVIDER`` names one.  Its URL is written as
    ``PRIVYX_UPSTREAM_URL`` writes it, which is also where a ``privyx run``
    profile's own provider is sent, without the credential.
    """
    if path[0] != "provider" or len(path) == 1:
        return path
    if path[1:] == ["base_url"]:
        return ["upstream_url"]
    return ["providers", provider, *path[1:]] if provider else path


def _aliases(merged: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, Any]]]:
    """The settings the aliases set, and each one's variable and value by dotted path.

    Empty values are left out, but for an anchor secret: an empty one means
    no anchor.
    """
    cfg: dict[str, Any] = {}
    given: dict[str, tuple[str, Any]] = {}
    provider = _env("PROVIDER")

    def put(variable: str, path: str, value: Any) -> None:
        given[path] = (variable, value)
        _put(cfg, _written(path.split("."), provider), value)

    agents = _env("AGENTS")
    if agents:
        try:
            parsed = json.loads(agents)
        except json.JSONDecodeError:
            raise ConfigError("PRIVYX_AGENTS must be a JSON object of agent profiles") from None
        if not isinstance(parsed, dict):
            raise ConfigError("PRIVYX_AGENTS must be a JSON object of agent profiles")
        cfg["agents"] = parsed
    # PRIVYX_PROVIDER selects a named provider; PRIVYX_API_KEY and
    # PRIVYX_UPSTREAM_URL are the key and URL of the provider selected above
    # the profiles (a profile's own provider does not take them).
    for variable, path in ALIASES.items():
        name = variable.removeprefix("PRIVYX_")
        if name == "AGENTS" or path in given:
            continue  # an SSL_ name wins over its TLS_ spelling
        if name == "ANCHOR_SECRET":
            if variable in os.environ:
                put(variable, path, _env(name))
            continue
        raw = _env(name)
        if not raw:
            continue
        if name in ("DETECTOR_CACHE", "AUDIT_ENABLED"):
            value: Any = _env_bool(name)
        elif name in ("PLUGIN_PATHS", "PLUGIN_MODULES"):
            value = _names(raw)
        else:
            value = raw  # validated with the rest, so a typo fails at startup
        if name == "DETECTOR_CACHE":
            entries = merged.get("detectors")
            for entry in entries if isinstance(entries, dict) else ["regex"]:
                put(variable, f"detectors.{entry}.cache", value)
        else:
            put(variable, path, value)
    return cfg, given


def _same(alias: Any, generic: Any, annotation: Any) -> bool:
    """Whether two values given for one setting are the same, read by its type."""
    if alias == generic:
        return True
    if annotation is UNKNOWN:
        return str(alias) == str(generic)
    try:
        adapter: TypeAdapter[Any] = TypeAdapter(annotation)
        return bool(adapter.validate_python(alias) == adapter.validate_python(generic))
    except (ValidationError, TypeError):
        return False


def _put(cfg: dict[str, Any], path: list[str], value: Any) -> None:
    for key in path[:-1]:
        nested = cfg.get(key)
        if not isinstance(nested, dict):
            nested = cfg[key] = {}
        cfg = nested
    cfg[path[-1]] = value
