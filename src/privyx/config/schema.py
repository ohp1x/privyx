"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class PluggableConfig(BaseModel):
    """A section whose ``type`` names a component: a built-in or a plugin's.

    The section's own fields are the ones Privyx applies whatever the type,
    each defined once in a mixin listed before this class, which comes last so
    that ``type`` is the first field.  Every other key is an option of the
    type, kept as written: the component's ``Options`` check it.
    :func:`~privyx.config.load_config` checks a built-in type's options
    and fills in their defaults.
    """

    model_config = ConfigDict(extra="allow")
    type: str = ""

    @classmethod
    def shared_fields(cls) -> list[str]:
        """The fields every type takes, beside its own options, in order."""
        return [name for name in cls.model_fields if name != "type"]


class VaultConfig(PluggableConfig):
    # Open string rather than a closed Literal so a plugin vault type validates;
    # building the vault still fails for a type that is neither built in nor
    # added by a plugin.
    type: str = "memory"


class DetectorCacheConfig(BaseModel):
    """Configuration for turn/detection caching.

    Caches entity detections for previously seen text spans, avoiding
    redundant regex or NLP scans across conversation turns.
    """

    enabled: bool = True
    max_size: int = 10000


class SharedDetectorOptions(BaseModel):
    """The settings every detector takes, whatever its type."""

    # `_` separates words, so acme is found in ACME_API_KEY (and the leak
    # check follows it, whatever the type)
    underscore_separates_words: bool = True
    cache: DetectorCacheConfig | bool = Field(default_factory=DetectorCacheConfig)

    @field_validator("cache", mode="before")
    @classmethod
    def _coerce_cache(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return DetectorCacheConfig(enabled=v)
        return v


class DetectorConfig(SharedDetectorOptions, PluggableConfig):
    """Detector selection and its rules."""

    type: str = "regex"


class PolicyConfig(PluggableConfig):
    type: str = "default"


class OperatorConfig(PluggableConfig):
    type: str = "pseudonym"


class AnchorConfig(PluggableConfig):
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


class SharedProviderOptions(BaseModel):
    """The settings every provider takes, whatever its type."""

    base_url: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class ProviderConfig(SharedProviderOptions, PluggableConfig):
    """Provider transport settings.

    ``base_url`` defaults to empty rather than to a URL: an empty value lets
    the provider registry fall back to the endpoint that provider type is
    documented to use (``api.openai.com`` for ``openai``, and so on).  A
    non-empty default here would silently override every such fallback.
    """

    type: str = "generic"


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
    # The upstream client, in both modes; see privyx.proxy._upstream.upstream_client.
    timeout: float = Field(default=300.0, gt=0)  # seconds between bytes, read or write
    connect_timeout: float = Field(default=10.0, gt=0)
    max_connections: int | None = Field(default=None, gt=0)  # unset: no limit
    #: Mask each copy of a value a request masked, and refuse the request if
    #: one is still where Privyx does not mask.
    dlp_check: bool = True
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
    #: :func:`privyx.providers.resolve_base_url` for the precedence.
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


#: Each section whose ``type`` names a component, by key: the kind of the component.
PLUGGABLE: dict[str, type[PluggableConfig]] = {
    "vault": VaultConfig,
    "detector": DetectorConfig,
    "policy": PolicyConfig,
    "operator": OperatorConfig,
    "anchor": AnchorConfig,
    "provider": ProviderConfig,
}
