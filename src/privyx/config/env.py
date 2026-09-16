"""Environment variable configuration helpers.

All environment variables are prefixed with ``PRIVYX_``.
"""

from __future__ import annotations

import json
import os
from typing import Any


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"PRIVYX_{key}", default)


def env_config() -> dict[str, Any]:
    """Load configuration from environment variables (non-empty only)."""
    cfg: dict[str, Any] = {}
    vault_type = _env("VAULT")
    if vault_type:
        cfg["vault"] = {"type": vault_type}
    dsn = _env("VAULT_DSN")
    if dsn:
        cfg.setdefault("vault", {})["dsn"] = dsn
    redis_url = _env("REDIS_URL")
    if redis_url:
        cfg.setdefault("vault", {})["redis_url"] = redis_url
    upstream = _env("UPSTREAM_URL")
    if upstream:
        cfg["upstream_url"] = upstream
        cfg.setdefault("provider", {})["base_url"] = upstream
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        value = _env(key)
        if value:
            provider_type = key.split("_")[0].lower()
            cfg.setdefault("provider", {})[f"{provider_type}_api_key"] = value
    anchor_secret = _env("ANCHOR_SECRET")
    if anchor_secret:
        cfg.setdefault("anchor", {})["secret"] = anchor_secret
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
        try:
            cfg["port"] = int(port)
        except ValueError:
            pass
    log_level = _env("LOG_LEVEL")
    if log_level:
        cfg["log_level"] = log_level
    logging_mode = _env("LOGGING")
    if logging_mode:
        cfg["logging"] = logging_mode
    audit_enabled = _env("AUDIT_ENABLED")
    if audit_enabled:
        cfg.setdefault("audit", {})["enabled"] = audit_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    audit_path = _env("AUDIT_PATH")
    if audit_path:
        cfg.setdefault("audit", {})["path"] = audit_path
    plugin_paths = _env("PLUGIN_PATHS")
    if plugin_paths:
        cfg["plugins"] = {"paths": [p.strip() for p in plugin_paths.split(",") if p.strip()]}
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