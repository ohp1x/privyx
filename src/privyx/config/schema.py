"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class VaultConfig(BaseModel):
    type: Literal["memory", "sqlite", "redis"] = "memory"
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build Settings from a (possibly partial) config dict."""
        merged = data
        return cls(**merged)