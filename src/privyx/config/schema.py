"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

#: A model of settings (``type`` is shadowed by a field inside the sections).
_Model = type[BaseModel]


class PluggableConfig(BaseModel):
    """A section whose ``type`` may name a plugin.

    Keys the schema does not define are kept: they are a plugin's own options,
    handed to its ``from_config``.  Under a built-in type they can only be typos,
    so :func:`~privyx.config.loader.load_config` rejects them there.

    A section's fields are the union of its built-in types' settings, each a
    mixin listed before this class, so a field is defined once.  This class
    comes last among the bases so that ``type`` is the first field.
    """

    model_config = ConfigDict(extra="allow")
    #: The ``type`` values Privyx implements itself, each to the settings it
    #: takes beside :attr:`shared_fields`.  A plugin never shadows one.
    builtin_types: ClassVar[dict[str, _Model]] = {}
    #: Fields Privyx applies itself, whatever the type, so a plugin's section
    #: takes them beside its own options.
    shared_fields: ClassVar[frozenset[str]] = frozenset()
    type: str = ""

    @classmethod
    def type_fields(cls, kind: str) -> frozenset[str]:
        """The fields built-in type ``kind`` uses: its own, ``type``, and the shared ones."""
        return frozenset({"type", *cls.shared_fields, *cls.builtin_types[kind].model_fields})


class NoSettings(BaseModel):
    """The settings of a built-in type that takes none of its own."""


class MemoryVaultConfig(BaseModel):
    """The settings of a ``memory`` vault."""

    # Idle expiry in seconds, measured from a session's last transform.  Unset
    # keeps sessions until deleted; 0 is rejected (it would expire every
    # session on creation, and Redis refuses it).
    ttl: int | None = Field(default=None, gt=0)


class SQLiteVaultConfig(MemoryVaultConfig):
    """The settings of a ``sqlite`` vault."""

    dsn: str = "sqlite+aiosqlite:///~/.privyx/privyx.db"


class RedisVaultConfig(MemoryVaultConfig):
    """The settings of a ``redis`` vault."""

    redis_url: str = "redis://localhost:6379/0"


class VaultConfig(RedisVaultConfig, SQLiteVaultConfig, PluggableConfig):
    builtin_types = {
        "memory": MemoryVaultConfig,
        "sqlite": SQLiteVaultConfig,
        "redis": RedisVaultConfig,
    }
    # Open string rather than a closed Literal so a plugin vault type validates;
    # build_vault still raises ConfigError for a type that is neither built-in
    # nor registered by a plugin.
    type: str = "memory"


class DetectorCacheConfig(BaseModel):
    """Configuration for turn/detection caching.

    Caches entity detections for previously seen text spans, avoiding
    redundant regex or NLP scans across conversation turns.
    """

    enabled: bool = True
    max_size: int = 10000


class PatternDetectorConfig(BaseModel):
    """The settings of a ``regex`` or ``yaml`` detector.

    ``patterns`` and ``terms`` are complementary and may both be set: the first
    is entity -> regex, the second entity -> a list of literal strings that
    Privyx escapes and compiles for you (case-insensitively).  They are merged
    into one pattern map; a hand-written ``patterns`` entry wins over a ``terms``
    entry for the same entity unless ``merge_terms`` is enabled.
    """

    patterns: dict[str, str] = Field(default_factory=dict)
    terms: dict[str, list[str]] = Field(default_factory=dict)
    merge_terms: bool = False  # match terms before patterns for the same entity
    term_variants: bool = False  # terms also match acme-corp, acme_corp, … for "Acme Corp"
    # entity -> words, one of which must be near a match
    context: dict[str, list[str]] = Field(default_factory=dict)
    enable: list[str] = Field(default_factory=list)  # opt-in built-in entities
    disable: list[str] = Field(default_factory=list)  # built-in entities to turn off
    entropy_threshold: float = Field(default=3.0, gt=0)  # HIGH_ENTROPY: bits per character
    decode: bool = False  # also scan split strings and Base64/hex/URL-encoded text
    normalize: bool = False  # also scan with fullwidth and zero-width characters folded


class PresidioDetectorConfig(BaseModel):
    """The settings of a ``presidio`` detector."""

    language: str = "en"
    model: str = ""  # spaCy model (empty → {language}_core_web_sm)
    entities: list[str] = Field(default_factory=list)  # empty → all recognizers
    score_threshold: float = 0.35  # minimum confidence


class LLMDetectorConfig(BaseModel):
    """The settings of an ``llm`` detector.

    Prefixed (unlike presidio's) because their unprefixed names would be
    ambiguous in the flat section: "provider" could read as the proxy's
    upstream ProviderConfig, and "model" already means a spaCy model name.
    """

    llm_provider: str = "openai"  # built-in SDK ("openai" or "anthropic")
    llm_model: str = ""  # model name (empty → a small default per provider)
    llm_api_key: str = ""  # empty defers to the SDK's own env lookup
    llm_instructions: str = ""  # prompt instructions (empty → built-in default)
    llm_timeout: float = Field(default=30.0, gt=0)  # seconds before a scan fails
    llm_max_chars: int = Field(default=4000, gt=0)  # longer text is chunked
    llm_fallback_on_error: bool = False  # regex-only scan instead of failing


class DetectorConfig(
    LLMDetectorConfig, PresidioDetectorConfig, PatternDetectorConfig, PluggableConfig
):
    """Detector selection and its rules."""

    builtin_types = {
        "regex": PatternDetectorConfig,
        "yaml": PatternDetectorConfig,
        "presidio": PresidioDetectorConfig,
        "llm": LLMDetectorConfig,
    }
    shared_fields = frozenset({"cache", "underscore_separates_words"})
    type: str = "regex"
    # `_` separates words, so acme is found in ACME_API_KEY (and the leak
    # check follows it, whatever the type)
    underscore_separates_words: bool = False
    cache: DetectorCacheConfig | bool = Field(default_factory=DetectorCacheConfig)

    @field_validator("cache", mode="before")
    @classmethod
    def _coerce_cache(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return DetectorCacheConfig(enabled=v)
        return v


class StrictPolicyConfig(BaseModel):
    """The settings of a ``strict`` policy."""

    allowed: list[str] = Field(default_factory=list)


class PolicyConfig(StrictPolicyConfig, PluggableConfig):
    builtin_types = {"default": NoSettings, "strict": StrictPolicyConfig}
    type: str = "default"


class RedactOperatorConfig(BaseModel):
    """The settings of a ``redact`` operator."""

    token: str = "[REDACTED]"


class HashOperatorConfig(BaseModel):
    """The settings of a ``hash`` operator."""

    length: int = Field(default=12, ge=1)  # digest length


class FPEOperatorConfig(BaseModel):
    """The settings of an ``fpe`` operator."""

    seed: int | None = None  # determinism salt


class FakerOperatorConfig(FPEOperatorConfig):
    """The settings of a ``faker`` operator, and of ``pseudonym`` labels."""

    locale: str = ""  # Faker locale (empty → Faker's default)


class EncryptOperatorConfig(BaseModel):
    """The settings of an ``encrypt`` operator."""

    key: str = ""  # 64 hex chars (`openssl rand -hex 32`)


class OperatorConfig(
    EncryptOperatorConfig,
    FakerOperatorConfig,
    HashOperatorConfig,
    RedactOperatorConfig,
    PluggableConfig,
):
    builtin_types = {
        "pseudonym": FakerOperatorConfig,
        "redact": RedactOperatorConfig,
        "hash": HashOperatorConfig,
        "faker": FakerOperatorConfig,
        "encrypt": EncryptOperatorConfig,
        "partial": NoSettings,
        "fpe": FPEOperatorConfig,
    }
    type: str = "pseudonym"


class HMACAnchorConfig(BaseModel):
    """The settings of an ``hmac`` anchor."""

    secret: str = ""


class AnchorConfig(HMACAnchorConfig, PluggableConfig):
    builtin_types = {"hmac": HMACAnchorConfig, "pasp": HMACAnchorConfig}
    type: str = "hmac"


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
    #: Issue short ``<E~1>`` tokens instead; ``format`` tokens still restore.
    compact: bool = False
    #: Hex characters in the id of an anchored ``pseudonym`` or ``encrypt``
    #: token; counters and ``hash`` (``operator.length``) are unaffected.
    id_length: int = Field(default=16, ge=6, le=64)
    #: Write ``pseudonym`` tokens with a fake value the model reads, such as
    #: ``<PRIVYX_PERSON_1:Alice Johnson>``; empty is off.  Needs ``{id}`` and
    #: ``{label}``.  Restored by the id; ``format`` tokens still restore.
    label_format: str = ""


class ProviderKeysConfig(BaseModel):
    """The key of each built-in provider type, which every built-in type takes.

    One environment can hold a key per type (``PRIVYX_OPENAI_API_KEY``, ...);
    the one for the selected type wins over ``api_key``.
    """

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""


class ProviderConfig(ProviderKeysConfig, PluggableConfig):
    """Provider transport settings.

    ``base_url`` defaults to empty rather than to a URL: an empty value lets
    the provider registry fall back to the endpoint that provider type is
    documented to use (``api.openai.com`` for ``openai``, and so on).  A
    non-empty default here would silently override every such fallback.
    """

    builtin_types = dict.fromkeys(("generic", "openai", "anthropic", "google"), ProviderKeysConfig)
    # The transparent proxy uses them whatever the type.
    shared_fields = frozenset({"base_url", "api_key", "headers"})
    type: str = "generic"
    base_url: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


def _default_routes() -> dict[str, str]:
    # Unlisted paths (files, batches) are forwarded without
    # pseudonymization; see docs/architecture/proxy.md.  The
    # paths without /v1 are what the OpenAI SDK calls when its base URL lacks
    # the /v1 it expects.  A `*` stands for the model in a Gemini path.  The
    # /api paths are Ollama's own API.
    # The first `*` is the API version (v1, v1beta, v1alpha).
    chat = ("generateContent", "streamGenerateContent", "countTokens")
    gemini = {
        **{f"/*/models/*:{m}": "gemini" for m in (*chat, "embedContent", "batchEmbedContents")},
        **{f"/*/tunedModels/*:{m}": "gemini" for m in chat},
        "/*/cachedContents": "gemini",
    }
    return {
        "/v1/chat/completions": "openai",
        "/v1/completions": "openai",
        "/v1/embeddings": "embeddings",
        "/v1/messages": "anthropic",
        "/v1/messages/count_tokens": "anthropic",
        "/v1/responses": "responses",
        "/v1/responses/input_tokens": "responses",
        "/v1/responses/compact": "responses",
        "/chat/completions": "openai",
        "/completions": "openai",
        "/embeddings": "embeddings",
        "/responses": "responses",
        "/responses/input_tokens": "responses",
        "/responses/compact": "responses",
        **gemini,
        "/api/chat": "ollama",
        "/api/generate": "ollama",
        "/api/embed": "embeddings",
        "/api/embeddings": "embeddings",
    }


class PathMappingConfig(BaseModel):
    """How the transparent proxy joins the upstream URL and a request path.

    ``origin`` keeps only the upstream's ``scheme://host[:port]`` and appends
    the request path.  ``prefix`` keeps the upstream's path as well and appends
    the request path after ``strip_prefix``, so a router at
    ``https://router.example.com/api/v1`` serves ``/v1/chat/completions`` at
    ``/api/v1/chat/completions``.  ``privyx run`` strips the agent's
    ``base_path``.  Requests outside ``strip_prefix`` are refused.
    """

    mode: Literal["origin", "prefix"] = "origin"
    strip_prefix: str = ""

    @field_validator("strip_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if value and (
            not value.startswith("/")
            or value.startswith("//")
            or any(c.isspace() or c in "?#%\\\x00" for c in value)
            or any(segment in (".", "..") for segment in value.split("/"))
        ):
            raise ValueError("must be empty or an absolute URL path without query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _prefix_mode(self) -> PathMappingConfig:
        if self.strip_prefix and self.mode != "prefix":
            raise ValueError("strip_prefix requires mode: prefix")
        return self


class ProxyConfig(BaseModel):
    """Proxy serving mode.

    ``mode`` selects which app :command:`privyx proxy` serves:

    - ``gateway`` — configured routes sent to a fixed upstream endpoint;
      embeddings and legacy completions use sibling endpoints. Native Ollama
      and OpenAI/Anthropic token-counting or compaction routes are not served.
      Gemini routes require the Google provider.
    - ``transparent`` — a drop-in reverse proxy.  Point a client's
      ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL`` at Privyx and every path is
      forwarded to the upstream; configured masking routes are pseudonymized
      on the way out and restored on the way back. ``path_mapping`` can keep
      an upstream path prefix.

    ``routes`` maps a request path to the wire schema used to transform it.
    In transparent mode, paths not listed are forwarded verbatim when
    ``passthrough_unknown`` is set (the default) and refused with 403 when it is
    not.  The gateway serves only the listed paths either way.
    """

    mode: Literal["gateway", "transparent"] = "transparent"
    routes: dict[str, str] = Field(default_factory=_default_routes)
    path_mapping: PathMappingConfig = Field(default_factory=PathMappingConfig)
    forward_client_auth: bool = True
    passthrough_unknown: bool = True
    # The upstream client, in both modes; see privyx.providers.generic.upstream_client.
    timeout: float = Field(default=300.0, gt=0)  # seconds between bytes, read or write
    connect_timeout: float = Field(default=10.0, gt=0)
    max_connections: int | None = Field(default=None, gt=0)  # unset: no limit
    #: Refuse a masked request that still holds, anywhere, a value it masked.
    dlp_check: bool = False
    #: Tell the model, in each masked chat request, to keep placeholders as written.
    directive: bool = False
    #: Mark a large Anthropic ``tools`` / ``system`` prefix for prompt caching
    #: when the request marks nothing itself.
    cache_control: bool = False

    @model_validator(mode="after")
    def _transparent_mapping(self) -> ProxyConfig:
        # The gateway calls a provider endpoint, not a joined path.
        if self.mode == "gateway" and self.path_mapping.mode != "origin":
            raise ValueError("path_mapping applies to transparent mode only")
        return self


class ModelPrice(BaseModel):
    """USD per million tokens; cache rates default to 0.1x and 1.25x of input."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)


class TracingConfig(BaseModel):
    """OpenTelemetry spans per proxied request (``pip install 'privyx[otel]'``).

    The exporter is configured by the standard ``OTEL_*`` environment variables.
    """

    enabled: bool = False


class AuditConfig(BaseModel):
    """PII-safe audit trail.

    Events (session created, transform, restore, proxy request) are appended as
    JSON lines to ``path``.  The trail records counts and entity *types*, never
    payload content; see :mod:`privyx.observability.audit`.
    """

    enabled: bool = True
    path: str = "~/.privyx/audit.log"
    #: Developer tap: each masked exchange before and after masking, one JSONL
    #: file per session in ``tap_dir``.  It holds original values; owner-only.
    tap: bool = False
    tap_dir: str = "~/.privyx/tap"
    #: Model-id prefix -> USD per million tokens, for ``privyx session show``.
    prices: dict[str, ModelPrice] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Plugin loading.

    Opt-in: nothing is loaded unless ``paths`` or ``modules`` name something.
    Each path, a directory or a ``.py`` file, is imported and scanned for
    concrete subclasses of the Privyx component base classes, which are
    registered under their ``name`` and become selectable as a component
    ``type``.  Each module, such as an installed package, is imported and its
    ``setup(px)`` called.  Installed packages are never loaded on their own:
    there is no entry-point discovery.  Set ``enabled: false`` to skip both.
    """

    enabled: bool = True
    paths: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)


class TLSConfig(BaseModel):
    """TLS/SSL server configuration for HTTPS termination."""

    certfile: str = ""
    keyfile: str = ""
    keyfile_password: str = ""
    ca_certs: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.certfile and self.keyfile)


class BudgetConfig(BaseModel):
    """Limits on one ``privyx run`` launch; unset means no limit."""

    max_turns: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentConfig(BaseModel):
    """How to launch a tool and point it at the local proxy; never a shell command."""

    command: str = Field(min_length=1)
    provider: str = Field(default="generic", min_length=1)
    upstream: str = ""
    env_vars: list[str] = Field(default_factory=list)
    #: Variables that may hold the tool's own endpoint, in order; empty means
    #: those of the built-in profile named like the command's file.
    endpoint_env: list[str] = Field(default_factory=list)
    base_path: str = ""
    extends: str | None = None
    adapter: Literal["auto", "env", "claude", "codex", "pi"] = "auto"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_from: dict[str, str] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    #: Names under ``presets`` applied left to right, below ``privyx``.
    presets: list[str] = Field(default_factory=list)
    #: Privyx settings for this profile, above config files and environment.
    privyx: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extends")
    @classmethod
    def _parent(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("must name a parent profile or be null")
        return value

    @field_validator("args")
    @classmethod
    def _args(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("arguments must contain no NUL")
        return values

    @field_validator("env", "env_from")
    @classmethod
    def _env_mapping(cls, values: dict[str, str], info: ValidationInfo) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in values):
            raise ValueError("keys must be environment variable names")
        if info.field_name == "env_from":
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in values.values()):
                raise ValueError("values must name source environment variables")
        elif any("\x00" in value for value in values.values()):
            raise ValueError("environment values must contain no NUL")
        return values

    @model_validator(mode="after")
    def _distinct_env(self) -> AgentConfig:
        if self.env.keys() & self.env_from.keys():
            raise ValueError("env and env_from must not define the same destination")
        return self

    @field_validator("command", "provider")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("must be non-blank and contain no NUL")
        return value

    @field_validator("env_vars", "endpoint_env")
    @classmethod
    def _env_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in values):
            raise ValueError("must contain environment variable names")
        return list(dict.fromkeys(values))

    @field_validator("base_path")
    @classmethod
    def _path(cls, value: str) -> str:
        if value and (
            not value.startswith("/")
            or value.startswith("//")
            or any(c.isspace() or c in "?#\x00" for c in value)
        ):
            raise ValueError("must be empty or an absolute URL path without query or fragment")
        return value

    @field_validator("upstream")
    @classmethod
    def _upstream(cls, value: str) -> str:
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("must be an HTTP(S) URL")
            if any(c.isspace() for c in value) or "\x00" in value:
                raise ValueError("must be an HTTP(S) URL without whitespace")
            # Access validates a malformed/out-of-range port too.
            _ = parts.port
        return value


def _default_agents() -> dict[str, AgentConfig]:
    from privyx.config.defaults import DEFAULT_AGENTS

    return {name: AgentConfig.model_validate(spec) for name, spec in DEFAULT_AGENTS.items()}


class Settings(BaseModel):
    """Top-level Privyx settings."""

    model_config = ConfigDict(hide_input_in_errors=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=0, le=65535)
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
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    tls: TLSConfig = Field(default_factory=TLSConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=_default_agents)
    #: Named sets of settings that a ``privyx run`` profile or ``--preset`` applies.
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: ``privyx run``: also mask the workspace's git authors, remote owners and
    #: names, and ``.env`` values, read at launch and kept in memory only.
    harvest: bool = False

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


#: Each section whose ``type`` may name a plugin, by key.
PLUGGABLE: dict[str, type[PluggableConfig]] = {
    "vault": VaultConfig,
    "detector": DetectorConfig,
    "policy": PolicyConfig,
    "operator": OperatorConfig,
    "anchor": AnchorConfig,
    "provider": ProviderConfig,
}

#: Where the published copy of :func:`json_schema` is served (``docs/schema.json``).
SCHEMA_URL = "https://ohp1x.github.io/privyx/schema.json"


def json_schema(
    plugins: dict[str, dict[str, type[BaseModel] | None]] | None = None,
) -> dict[str, Any]:
    """The JSON Schema of a config file, for editors that complete and check YAML.

    A section that is not pluggable takes no other keys, as in
    :func:`~privyx.config.loader.load_config`.  A pluggable one takes, under a
    built-in type, only the settings of that type and the shared ones, and any
    key under another, since a plugin type has options of its own.

    ``plugins`` maps a section to the plugin types loaded for it, each to its
    ``Config`` or ``None``.  With it, ``type`` takes only the built-in and
    loaded names, and a plugin with a ``Config`` only its options.
    """
    schema = Settings.model_json_schema()
    schema["properties"]["include"] = {
        "description": "Config files or globs to merge below this one, relative to it.",
        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
    }
    for section in [schema, *schema["$defs"].values()]:
        section.setdefault("additionalProperties", False)
    # A profile may take its command from the profile it extends.
    schema["$defs"]["AgentConfig"].pop("required", None)
    _allow_references(schema)
    for name, model in PLUGGABLE.items():
        loaded = None if plugins is None else plugins.get(name, {})
        _by_type(schema["$defs"], name, model, loaded)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_URL, **schema}


def _by_type(
    defs: dict[str, Any],
    section: str,
    model: type[PluggableConfig],
    loaded: dict[str, type[BaseModel] | None] | None,
) -> None:
    """Make ``model``'s definition check each ``type`` with that type's settings.

    The checks are ``if``/``then`` pairs, so a ``${NAME}`` type, which matches
    no name, is checked as an unknown plugin's: with any key.
    """
    node = defs[model.__name__]
    builtin = sorted(model.builtin_types)
    # A plugin never shadows a built-in type, so one named like it gets no check.
    plugins = {name: options for name, options in (loaded or {}).items() if name not in builtin}
    names = builtin if loaded is None else builtin + sorted(plugins)
    kind = {k: v for k, v in node["properties"]["type"].items() if k in ("default", "title")}
    if loaded is None:  # suggest the built-in names, take any other
        kind["anyOf"] = [{"enum": names}, {"type": "string"}]
    else:
        kind["anyOf"] = [{"enum": names}, {"type": "string", "pattern": r"\$\{"}]
    props = node["properties"]
    # Only `type` and the shared fields apply whatever the type; each built-in
    # type adds its own below, so an editor offers only those.
    node["properties"] = {"type": kind, **{k: props[k] for k in props if k in model.shared_fields}}
    node["additionalProperties"] = True
    taken = {"type": True, **dict.fromkeys(model.shared_fields, True)}
    default = model.model_fields["type"].default
    groups: dict[type[BaseModel], list[str]] = {}
    for name, settings in model.builtin_types.items():
        groups.setdefault(settings, []).append(name)
    checks: list[dict[str, Any]] = []
    for settings, types in groups.items():
        check: dict[str, Any] = {"properties": {"type": {"enum": types}}}
        if default not in types:  # the default's check also applies with no `type`
            check["required"] = ["type"]
        own = {key: props[key] for key in settings.model_fields}
        checks.append(
            {"if": check, "then": {"properties": {**taken, **own}, "additionalProperties": False}}
        )
    for name, options in sorted(plugins.items()):
        if options is None:
            continue  # no Config: any key is the plugin's to read
        prefix = f"{section}.{name}."
        own = options.model_json_schema(ref_template=f"#/$defs/{prefix}{{model}}")
        for key, value in own.pop("$defs", {}).items():
            defs[prefix + key] = value
        _allow_references(own)
        checks.append(
            {
                "if": {"properties": {"type": {"const": name}}, "required": ["type"]},
                "then": {
                    "properties": {**taken, **own.get("properties", {})},
                    "additionalProperties": False,
                },
            }
        )
    node["allOf"] = checks


def _allow_references(node: Any) -> None:
    """Let a number, boolean, or enum be a ``${NAME}`` reference, which the loader resolves."""
    if isinstance(node, list):
        for item in node:
            _allow_references(item)
    elif isinstance(node, dict):
        restricted = "enum" in node or "const" in node
        if restricted or node.get("type") in ("integer", "number", "boolean"):
            value = {k: node.pop(k) for k in list(node) if k not in ("title", "description")}
            node["anyOf"] = [value, {"type": "string", "pattern": r"\$\{"}]
        else:
            for item in node.values():
                _allow_references(item)
