"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class VaultConfig(BaseModel):
    # Open string rather than a closed Literal so a plugin vault type validates;
    # build_vault still raises ConfigError for a type that is neither built-in
    # nor registered by a plugin.
    type: str = "memory"
    dsn: str = "sqlite+aiosqlite:///privyx.db"
    redis_url: str = "redis://localhost:6379/0"
    ttl: int | None = None


class DetectorConfig(BaseModel):
    type: str = "regex"
    patterns: dict[str, str] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    type: str = "default"
    allowed: list[str] = Field(default_factory=list)


class OperatorConfig(BaseModel):
    type: str = "pseudonym"
    token: str = "[REDACTED]"  # redact operator
    length: int = 12  # hash operator digest length


class AnchorConfig(BaseModel):
    type: str = "hmac"
    secret: str = ""


class TokenConfig(BaseModel):
    """How placeholder tokens are serialized in text.

    ``format`` must contain ``{type}`` and ``{id}``; ``{namespace}`` is optional.
    Adjacent placeholders need a delimiter between them.  The default reproduces
    the historical ``<PRIVYX_EMAIL_1>`` syntax; alternatives include
    ``[[{namespace}:{type}:{id}]]`` and ``<{namespace}:{type}:{id}>``.
    """

    namespace: str = "PRIVYX"
    format: str = "<{namespace}_{type}_{id}>"


class ProviderConfig(BaseModel):
    """Provider transport settings.

    ``base_url`` defaults to empty rather than to a URL: an empty value lets
    the provider registry fall back to the endpoint that provider type is
    documented to use (``api.openai.com`` for ``openai``, and so on).  A
    non-empty default here would silently override every such fallback.
    """

    type: str = "generic"
    base_url: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""


def _default_routes() -> dict[str, str]:
    return {
        "/v1/chat/completions": "openai",
        "/v1/messages": "anthropic",
    }


class ProxyConfig(BaseModel):
    """Proxy serving mode.

    ``mode`` selects which app :command:`privyx proxy` serves:

    - ``gateway`` — the narrow, chat-only gateway (``/v1/chat/completions``),
      OpenAI schema only.
    - ``transparent`` — a drop-in reverse proxy.  Point a client's
      ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL`` at Privyx and every path is
      forwarded to the upstream origin; chat paths are pseudonymized on the way
      out and restored on the way back, all without client-side changes.

    ``routes`` maps a request path to the wire schema used to transform it.
    Paths not listed are forwarded verbatim when ``passthrough_unknown`` is set.
    """

    mode: Literal["gateway", "transparent"] = "transparent"
    routes: dict[str, str] = Field(default_factory=_default_routes)
    forward_client_auth: bool = True
    passthrough_unknown: bool = True


class AuditConfig(BaseModel):
    """PII-safe audit trail.

    Events (session created, transform, restore, proxy request) are appended as
    JSON lines to ``path``.  The trail records counts and entity *types*, never
    payload content; see :mod:`privyx.observability.audit`.
    """

    enabled: bool = True
    path: str = "privyx-audit.log"


class PluginsConfig(BaseModel):
    """Local plugin discovery.

    Opt-in: nothing is loaded unless ``paths`` lists a directory or a ``.py``
    file.  Each path is imported and scanned for concrete subclasses of the
    Privyx component base classes, which are registered under their ``name`` and
    become selectable as a component ``type``.  Discovery is local to this
    deployment — there is no third-party entry-point mechanism.  Set
    ``enabled: false`` to skip loading even when paths are configured.
    """

    enabled: bool = True
    paths: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    """Top-level Privyx settings."""

    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    logging: str = "text"
    #: Explicit upstream override (``--upstream`` / ``PRIVYX_UPSTREAM_URL``).
    #: Empty means "use whatever the configured provider resolves to"; see
    #: :func:`privyx.providers.registry.build_provider` for the precedence.
    upstream_url: str = ""
    vault: VaultConfig = Field(default_factory=VaultConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    operator: OperatorConfig = Field(default_factory=OperatorConfig)
    anchor: AnchorConfig = Field(default_factory=AnchorConfig)
    token: TokenConfig = Field(default_factory=TokenConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build Settings from a (possibly partial) config dict."""
        merged = data
        return cls(**merged)