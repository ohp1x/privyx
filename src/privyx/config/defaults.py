"""Default configuration values.

Entity patterns are imported from :mod:`privyx.privacy.detector.builtin` rather
than duplicated, so the code-level defaults and the config-level defaults
cannot drift apart.
"""

from __future__ import annotations

from privyx.privacy.detector.builtin import DEFAULT_PATTERNS

DEFAULTS: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 8000,
    "log_level": "info",
    "logging": "text",
    # Empty on purpose: this is the explicit override, and a value here would
    # make `provider.base_url` and the per-provider defaults unreachable.
    "upstream_url": "",
    "vault": {
        "type": "memory",
        "dsn": "sqlite+aiosqlite:///privyx.db",
        "redis_url": "redis://localhost:6379/0",
        "ttl": None,
    },
    "detector": {
        "type": "regex",
        "patterns": dict(DEFAULT_PATTERNS),
    },
    "policy": {"type": "default"},
    "operator": {"type": "pseudonym"},
    "anchor": {"type": "hmac", "secret": ""},
    "token": {"namespace": "PRIVYX", "format": "<{namespace}_{type}_{id}>"},
    "provider": {
        "type": "generic",
        # Also empty: an unset base_url resolves to the provider type's own
        # default (see privyx.providers.registry.DEFAULT_BASE_URLS).
        "base_url": "",
        "api_key": "",
        "headers": {},
    },
    "audit": {
        "enabled": True,
        "path": "privyx-audit.log",
    },
    # Opt-in: no paths means no plugins are loaded.
    "plugins": {"enabled": True, "paths": []},
}
