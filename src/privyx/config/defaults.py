"""Default configuration values."""

from __future__ import annotations

#: The built-in named providers: one per built-in type, and the two
#: OpenAI-compatible and generic hosts the built-in profiles use.  A key
#: written for one is sent to its host only.
DEFAULT_PROVIDERS: dict[str, dict[str, object]] = {
    "generic": {"type": "generic"},
    "openai": {"type": "openai"},
    "anthropic": {"type": "anthropic"},
    "google": {"type": "google"},
    "xai": {"type": "openai", "base_url": "https://api.x.ai/v1"},
    "ollama": {"type": "generic", "base_url": "http://127.0.0.1:11434"},
}

DEFAULT_AGENTS: dict[str, dict[str, object]] = {
    "claude": {
        "command": "claude",
        "type": "claude",
        "provider": "anthropic",
        "env_vars": ["ANTHROPIC_BASE_URL"],
    },
    "codex": {
        "command": "codex",
        "type": "codex",
        "provider": "openai",
        "env_vars": ["OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "openai": {
        "command": "openai",
        "type": "env",
        "provider": "openai",
        "env_vars": ["OPENAI_BASE_URL"],
        "endpoint_env": ["OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "aider": {
        "command": "aider",
        "type": "env",
        "provider": "openai",
        "env_vars": ["OPENAI_API_BASE", "OPENAI_BASE_URL"],
        "endpoint_env": ["OPENAI_API_BASE", "OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "pi": {
        "command": "pi",
        "type": "pi",
        "provider": "anthropic",
        "env_vars": ["PRIVYX_PI_BASE_URL"],
    },
    # Its Anthropic and OpenAI providers both read a base URL ending in /v1;
    # pick the upstream of the model's provider with --provider.
    "opencode": {
        "command": "opencode",
        "type": "env",
        "provider": "anthropic",
        "env_vars": ["ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    # API-key mode only.  Its Google-login (Code Assist) and Vertex endpoints
    # point at Privyx too, which refuses their paths (403), so those modes fail
    # rather than reach Google unmasked; they are never read as its endpoint.
    "gemini": {
        "command": "gemini",
        "type": "env",
        "provider": "google",
        "env_vars": ["GOOGLE_GEMINI_BASE_URL", "CODE_ASSIST_ENDPOINT", "GOOGLE_VERTEX_BASE_URL"],
        "endpoint_env": ["GOOGLE_GEMINI_BASE_URL"],
        "privyx": {"proxy": {"passthrough_unknown": False}},
    },
    # `ollama:<model>` runs `ollama run <model>`; the upstream is the local
    # server, or the one OLLAMA_HOST names (see privyx.agents.OllamaAgent).
    "ollama": {
        "command": "ollama",
        "type": "ollama",
        "provider": "ollama",
        "env_vars": ["OLLAMA_HOST"],
    },
    # xAI's API is OpenAI-compatible, but not OpenAI's: Grok Build sends its
    # own xAI key, and an OpenAI key of Privyx's stays with provider openai.
    "grok": {
        "command": "grok",
        "type": "env",
        "provider": "xai",
        "env_vars": ["GROK_XAI_API_BASE_URL"],
        "endpoint_env": ["GROK_XAI_API_BASE_URL"],
        "base_path": "/v1",
    },
}

#: The built-in features, each on or off unless a config says otherwise.
#: ``budget`` is a ``privyx run`` profile's.
DEFAULT_FEATURES: dict[str, bool] = {
    "audit": True,
    "metrics": True,
    "tracing": False,
    "directive": False,
    "cache_control": False,
}

DEFAULTS: dict[str, object] = {
    "agents": DEFAULT_AGENTS,
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "workers": 1,
        # TLS / HTTPS termination: empty paths mean plain HTTP.
        "tls": {"certfile": "", "keyfile": "", "keyfile_password": "", "ca_certs": ""},
    },
    "log": {"level": "info", "format": "text", "file": ""},
    # A pluggable section names its type only: a field set here would count
    # as set in the config, and reach a plugin's Config in place of its own
    # default.  The fields' defaults are in privyx.config.schema.
    "vault": {"type": "memory"},
    # No built-in patterns: the `regex` detector adds them itself, unless
    # `builtins: false`.
    "detectors": {"regex": {}},
    "policy": {"type": "default"},
    "operator": {"type": "pseudonym"},
    "anchor": {"type": "hmac"},
    # Session identity when the client sends no x-privyx-session header.
    # ephemeral (default) is safe and unchanged; see SessionConfig for the rest.
    "session": {"strategy": "ephemeral"},
    "token": {"namespace": "PRIVYX", "format": "<{namespace}_{type}_{id}>"},
    # An unset base_url resolves to the provider type's own default (see
    # privyx.providers.DEFAULT_BASE_URLS).
    "provider": {"type": "generic"},
    "providers": DEFAULT_PROVIDERS,
    "features": {name: {"enabled": on} for name, on in DEFAULT_FEATURES.items()},
    # Opt-in: no paths means no plugins are loaded.
    "plugins": {"enabled": True, "paths": [], "modules": []},
}
