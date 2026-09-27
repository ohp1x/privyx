"""Default configuration values."""

from __future__ import annotations

DEFAULTS: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 8000,
    "log_level": "info",
    "logging": "text",
    "log_file": "",
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
        # Empty: the `regex` detector adds the built-in patterns itself.  A copy
        # here would be merged into a `yaml` detector, which must see only the
        # patterns it is given.
        "patterns": {},
        # Literal term lists (entity -> list of strings); empty by default.
        "terms": {},
    },
    "policy": {"type": "default"},
    "operator": {"type": "pseudonym"},
    "anchor": {"type": "hmac", "secret": ""},
    # Session identity when the client sends no x-privyx-session header.
    # ephemeral (default) is safe and unchanged; see SessionConfig for the rest.
    "session": {"strategy": "ephemeral"},
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
    # Server TLS / HTTPS termination: empty paths mean plain HTTP.
    "tls": {
        "certfile": "",
        "keyfile": "",
        "keyfile_password": "",
        "ca_certs": "",
    },
}
