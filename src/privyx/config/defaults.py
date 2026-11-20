"""Default configuration values."""

from __future__ import annotations

#: Every key of Privyx's own, each meant for its provider type's host.  A
#: profile whose upstream is another host clears them all: whichever type is
#: in effect, the tool's own credentials reach that host instead.
_NO_KEYS = {"api_key": "", "openai_api_key": "", "anthropic_api_key": "", "google_api_key": ""}

DEFAULT_AGENTS: dict[str, dict[str, object]] = {
    "claude": {
        "command": "claude",
        "adapter": "claude",
        "provider": "anthropic",
        "env_vars": ["ANTHROPIC_BASE_URL"],
    },
    "codex": {
        "command": "codex",
        "adapter": "codex",
        "provider": "openai",
        "env_vars": ["OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "openai": {
        "command": "openai",
        "provider": "openai",
        "env_vars": ["OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "aider": {
        "command": "aider",
        "provider": "openai",
        "env_vars": ["OPENAI_API_BASE", "OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    "pi": {
        "command": "pi",
        "adapter": "pi",
        "provider": "anthropic",
        "env_vars": ["PRIVYX_PI_BASE_URL"],
    },
    # Its Anthropic and OpenAI providers both read a base URL ending in /v1;
    # pick the upstream of the model's provider with --provider.
    "opencode": {
        "command": "opencode",
        "provider": "anthropic",
        "env_vars": ["ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"],
        "base_path": "/v1",
    },
    # API-key mode only.  Its Google-login (Code Assist) and Vertex endpoints
    # point at Privyx too, which refuses their paths (403), so those modes fail
    # rather than reach Google unmasked.
    "gemini": {
        "command": "gemini",
        "provider": "google",
        "env_vars": ["GOOGLE_GEMINI_BASE_URL", "CODE_ASSIST_ENDPOINT", "GOOGLE_VERTEX_BASE_URL"],
        "privyx": {"proxy": {"passthrough_unknown": False}},
    },
    # `ollama:<model>` runs `ollama run <model>`; the upstream is the local
    # server, or the one OLLAMA_HOST names (see privyx.cli.launch.ollama_url).
    # Ollama takes no key, so none of Privyx's own is sent to that server.
    "ollama": {
        "command": "ollama",
        "upstream": "http://127.0.0.1:11434",
        "env_vars": ["OLLAMA_HOST"],
        "privyx": {"provider": dict(_NO_KEYS)},
    },
    # xAI's API is OpenAI-compatible, but not OpenAI's: Grok Build sends its
    # own xAI key.
    "grok": {
        "command": "grok",
        "provider": "openai",
        "upstream": "https://api.x.ai/v1",
        "env_vars": ["GROK_XAI_API_BASE_URL"],
        "base_path": "/v1",
        "privyx": {"provider": dict(_NO_KEYS)},
    },
}

DEFAULTS: dict[str, object] = {
    "agents": DEFAULT_AGENTS,
    "host": "127.0.0.1",
    "port": 8000,
    "log_level": "info",
    "logging": "text",
    "log_file": "",
    # Empty on purpose: this is the explicit override, and a value here would
    # make `provider.base_url` and the per-provider defaults unreachable.
    "upstream_url": "",
    # A pluggable section names its type only: a field set here would count
    # as set in the config, and reach a plugin's Config in place of its own
    # default.  The fields' defaults are in privyx.config.schema.
    "vault": {"type": "memory"},
    # No built-in patterns: the `regex` detector adds them itself.  A copy here
    # would be merged into a `yaml` detector, which must see only its own.
    "detector": {"type": "regex"},
    "policy": {"type": "default"},
    "operator": {"type": "pseudonym"},
    "anchor": {"type": "hmac"},
    # Session identity when the client sends no x-privyx-session header.
    # ephemeral (default) is safe and unchanged; see SessionConfig for the rest.
    "session": {"strategy": "ephemeral"},
    "token": {"namespace": "PRIVYX", "format": "<{namespace}_{type}_{id}>"},
    # An unset base_url resolves to the provider type's own default (see
    # privyx.providers.registry.DEFAULT_BASE_URLS).
    "provider": {"type": "generic"},
    "audit": {
        "enabled": True,
        "path": "~/.privyx/audit.log",
    },
    # Opt-in: no paths means no plugins are loaded.
    "plugins": {"enabled": True, "paths": [], "modules": []},
    # Server TLS / HTTPS termination: empty paths mean plain HTTP.
    "tls": {
        "certfile": "",
        "keyfile": "",
        "keyfile_password": "",
        "ca_certs": "",
    },
}
