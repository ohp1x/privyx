"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class VaultConfig(BaseModel):
    # Open string rather than a closed Literal so a plugin vault type validates;
    # build_vault still raises ConfigError for a type that is neither built-in
    # nor registered by a plugin.
    type: str = "memory"
    dsn: str = "sqlite+aiosqlite:///privyx.db"
    redis_url: str = "redis://localhost:6379/0"
    ttl: int | None = None


class DetectorCacheConfig(BaseModel):
    """Configuration for turn/detection caching.

    Caches entity detections for previously seen text spans, avoiding
    redundant regex or NLP scans across conversation turns.
    """

    enabled: bool = True
    max_size: int = 10000


class DetectorConfig(BaseModel):
    """Detector selection and its rules.

    ``patterns`` and ``terms`` are complementary and may both be set: the first
    is entity -> regex, the second entity -> a list of literal strings that
    Privyx escapes and compiles for you (case-insensitively).  They are merged
    into one pattern map; a hand-written ``patterns`` entry wins over a ``terms``
    entry for the same entity.
    """

    type: str = "regex"
    patterns: dict[str, str] = Field(default_factory=dict)
    terms: dict[str, list[str]] = Field(default_factory=dict)
    cache: DetectorCacheConfig | bool = Field(default_factory=DetectorCacheConfig)
    language: str = "en"  # presidio detector
    model: str = ""  # presidio detector: spaCy model (empty → {language}_core_web_sm)
    entities: list[str] = Field(default_factory=list)  # presidio: empty → all recognizers
    score_threshold: float = 0.35  # presidio detector: minimum confidence
    # llm detector fields are prefixed (unlike presidio's) because their
    # unprefixed names would be ambiguous here: "provider" could read as the
    # proxy's upstream ProviderConfig, and "model" already means a spaCy model
    # name for presidio in this same schema.
    llm_provider: str = "openai"  # llm detector: built-in SDK ("openai" or "anthropic")
    llm_model: str = ""  # llm detector: model name (empty → a small default per provider)
    llm_api_key: str = ""  # llm detector: empty defers to the SDK's own env lookup
    llm_instructions: str = ""  # llm detector: prompt instructions (empty → built-in default)

    @field_validator("cache", mode="before")
    @classmethod
    def _coerce_cache(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return DetectorCacheConfig(enabled=v)
        return v


class PolicyConfig(BaseModel):
    type: str = "default"
    allowed: list[str] = Field(default_factory=list)


class OperatorConfig(BaseModel):
    type: str = "pseudonym"
    token: str = "[REDACTED]"  # redact operator
    length: int = 12  # hash operator digest length
    locale: str = ""  # faker operator: Faker locale (empty → Faker's default)
    seed: int | None = None  # faker operator: determinism salt
    key: str = ""  # encrypt operator: 64 hex chars (`openssl rand -hex 32`)


class AnchorConfig(BaseModel):
    type: str = "hmac"
    secret: str = ""


class SessionConfig(BaseModel):
    """How a session is identified when the client sends no ``x-privyx-session``.

    An explicit ``x-privyx-session`` header always wins.  Absent one, ``strategy``
    decides the fallback:

    - ``ephemeral`` (default) — a fresh session per request.  Nothing is shared
      between requests, so two callers' pseudonym maps can never collide, but a
      multi-turn conversation is re-tokenized from scratch every turn and shows up
      as many sessions.
    - ``client`` — a stable session derived from the client credential
      (``Authorization`` / ``x-api-key``): one API key → one reused session.
      Simple, but every conversation under that key shares one pseudonym map.
    - ``conversation`` — a stable session derived from the credential *and* the
      first user message, so one conversation → one reused session while different
      conversations (even under the same key) stay isolated.

    Both derived strategies fall back to ``ephemeral`` when their signal is absent
    (e.g. a request carries no credential).
    """

    strategy: Literal["ephemeral", "client", "conversation"] = "ephemeral"


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
    # Unlisted paths (embeddings, legacy /v1/completions, Gemini-native) are
    # forwarded without pseudonymization; see docs/architecture/proxy.md.
    return {
        "/v1/chat/completions": "openai",
        "/v1/messages": "anthropic",
        "/v1/messages/count_tokens": "anthropic",
        "/v1/responses": "responses",
        "/v1/responses/input_tokens": "responses",
        "/v1/responses/compact": "responses",
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
    In transparent mode, paths not listed are forwarded verbatim when
    ``passthrough_unknown`` is set (the default) and refused with 403 when it is
    not.  The gateway serves only the listed paths either way.
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


class TLSConfig(BaseModel):
    """TLS/SSL server configuration for HTTPS termination."""

    certfile: str = ""
    keyfile: str = ""
    keyfile_password: str = ""
    ca_certs: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.certfile and self.keyfile)


class Settings(BaseModel):
    """Top-level Privyx settings."""

    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    logging: str = "text"
    log_file: str = ""
    #: Explicit upstream override (``--upstream`` / ``PRIVYX_UPSTREAM_URL``).
    #: Empty means "use whatever the configured provider resolves to"; see
    #: :func:`privyx.providers.registry.build_provider` for the precedence.
    upstream_url: str = ""
    vault: VaultConfig = Field(default_factory=VaultConfig)
    #: One detector, or a list of them run together (their spans are pooled).
    detector: DetectorConfig | list[DetectorConfig] = Field(default_factory=DetectorConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    operator: OperatorConfig = Field(default_factory=OperatorConfig)
    anchor: AnchorConfig = Field(default_factory=AnchorConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    token: TokenConfig = Field(default_factory=TokenConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    tls: TLSConfig = Field(default_factory=TLSConfig)

    @property
    def is_tls(self) -> bool:
        """Whether TLS/SSL is active for the proxy server."""
        return self.tls.enabled

    @property
    def detector_type(self) -> str:
        """The detector type for display, e.g. ``regex`` or ``regex+presidio``."""
        if isinstance(self.detector, list):
            return "+".join(d.type for d in self.detector)
        return self.detector.type

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build Settings from a (possibly partial) config dict."""
        merged = data
        return cls(**merged)
