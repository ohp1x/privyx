"""Environment variable configuration helpers.

All environment variables are prefixed with ``PRIVYX_``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from privyx.core import ConfigError


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


def env_config() -> dict[str, Any]:
    """Load non-empty environment values, allowing an empty anchor secret."""
    cfg: dict[str, Any] = {}
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
    provider = _env("PROVIDER")
    api_key = _env("API_KEY")
    if provider:
        cfg["provider"] = provider
        if api_key:
            cfg["providers"] = {provider: {"api_key": api_key}}
    elif api_key:
        cfg["provider"] = {"api_key": api_key}
    vault_type = _env("VAULT")
    if vault_type:
        cfg["vault"] = {"type": vault_type}
    dsn = _env("VAULT_DSN")
    if dsn:
        cfg.setdefault("vault", {})["dsn"] = dsn
    redis_url = _env("REDIS_URL")
    if redis_url:
        cfg.setdefault("vault", {})["redis_url"] = redis_url
    vault_ttl = _env("VAULT_TTL")
    if vault_ttl:
        # Validated with the rest: a typo fails at startup instead of leaving
        # sessions, with their original values, to never expire.
        cfg.setdefault("vault", {})["ttl"] = vault_ttl
    upstream = _env("UPSTREAM_URL")
    if upstream:
        cfg["upstream_url"] = upstream  # the base URL of the provider selected
    for name in ("openai", "anthropic", "google"):
        # The key of the built-in provider of that name, for its own host only.
        value = _env(f"{name.upper()}_API_KEY")
        if value:
            cfg.setdefault("providers", {}).setdefault(name, {})["api_key"] = value
    if "PRIVYX_ANCHOR_SECRET" in os.environ:
        cfg.setdefault("anchor", {})["secret"] = _env("ANCHOR_SECRET")
    session_strategy = _env("SESSION_STRATEGY")
    if session_strategy:
        cfg.setdefault("session", {})["strategy"] = session_strategy
    encrypt_key = _env("ENCRYPT_KEY")
    if encrypt_key:
        cfg.setdefault("operator", {})["key"] = encrypt_key
    token_format = _env("TOKEN_FORMAT")
    if token_format:
        cfg.setdefault("token", {})["format"] = token_format
    token_namespace = _env("TOKEN_NAMESPACE")
    if token_namespace:
        cfg.setdefault("token", {})["namespace"] = token_namespace
    host = _env("HOST")
    if host:
        cfg["host"] = host
    port = _env("PORT")
    if port:
        cfg["port"] = port  # validated with the rest, so a typo fails at startup
    log_level = _env("LOG_LEVEL")
    if log_level:
        cfg["log_level"] = log_level
    log_file = _env("LOG_FILE")
    if log_file:
        cfg["log_file"] = log_file
    logging_mode = _env("LOGGING")
    if logging_mode:
        cfg["logging"] = logging_mode
    if _env("AUDIT_ENABLED"):
        cfg.setdefault("audit", {})["enabled"] = _env_bool("AUDIT_ENABLED")
    audit_path = _env("AUDIT_PATH")
    if audit_path:
        cfg.setdefault("audit", {})["path"] = audit_path
    plugin_paths = _env("PLUGIN_PATHS")
    if plugin_paths:
        cfg.setdefault("plugins", {})["paths"] = _names(plugin_paths)
    plugin_modules = _env("PLUGIN_MODULES")
    if plugin_modules:
        cfg.setdefault("plugins", {})["modules"] = _names(plugin_modules)
    if _env("DETECTOR_CACHE"):
        cfg.setdefault("detector", {})["cache"] = _env_bool("DETECTOR_CACHE")
    ssl_certfile = _env("SSL_CERTFILE") or _env("TLS_CERTFILE")
    if ssl_certfile:
        cfg.setdefault("tls", {})["certfile"] = ssl_certfile
    ssl_keyfile = _env("SSL_KEYFILE") or _env("TLS_KEYFILE")
    if ssl_keyfile:
        cfg.setdefault("tls", {})["keyfile"] = ssl_keyfile
    ssl_keyfile_password = _env("SSL_KEYFILE_PASSWORD") or _env("TLS_KEYFILE_PASSWORD")
    if ssl_keyfile_password:
        cfg.setdefault("tls", {})["keyfile_password"] = ssl_keyfile_password
    ssl_ca_certs = _env("SSL_CA_CERTS") or _env("TLS_CA_CERTS")
    if ssl_ca_certs:
        cfg.setdefault("tls", {})["ca_certs"] = ssl_ca_certs
    return cfg


def parse_json_env(key: str) -> Any | None:
    """Parse a JSON-encoded environment variable, or return None."""
    raw = os.environ.get(f"PRIVYX_{key}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
