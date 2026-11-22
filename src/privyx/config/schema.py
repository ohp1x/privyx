"""Configuration schema and typed settings (pydantic)."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)


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


class Switch(BaseModel):
    """The setting of an entry of ``detectors`` or ``features`` that Privyx applies.

    Writing ``false`` in place of the entry is ``enabled: false``, and ``true``
    is ``enabled: true``, so a layer turns an entry off or on again without
    the options a layer below gave it.
    """

    enabled: bool = True


class DetectorConfig(SharedDetectorOptions, Switch, PluggableConfig):
    """One of ``detectors``: its type, by default the entry's name, and its rules."""


class FeatureConfig(Switch, PluggableConfig):
    """One of ``features``: its type, by default the entry's name, and its options."""


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
    #: Write ``pseudonym`` tokens with a fake value the model reads, such as
    #: ``<PRIVYX_PERSON_1:Alice Johnson>``; empty is off.  Needs ``{id}`` and
    #: ``{label}``.  Restored by the id; ``format`` tokens still restore.
    label_format: str = ""


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


class SharedProviderOptions(BaseModel):
    """The settings every provider takes, whatever its type: where requests go, and how.

    A credential, ``api_key`` or a header, is sent only to the origin of the
    ``base_url`` of the definition it is written in, or to its type's default
    URL when the definition writes none.
    """

    base_url: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    #: In transparent mode, relay the client's own key when Privyx holds none.
    forward_client_auth: bool = True
    path_mapping: PathMappingConfig = Field(default_factory=PathMappingConfig)
    # The upstream client, in both modes; see privyx.proxy._upstream.upstream_client.
    timeout: float = Field(default=300.0, gt=0)  # seconds between bytes, read or write
    connect_timeout: float = Field(default=10.0, gt=0)
    max_connections: int | None = Field(default=None, gt=0)  # unset: no limit


class ProviderConfig(SharedProviderOptions, PluggableConfig):
    """An upstream connection: the top-level ``provider``, or one of ``providers``.

    ``base_url`` defaults to empty rather than to a URL: an empty value lets
    the provider registry fall back to the endpoint that provider type is
    documented to use (``api.openai.com`` for ``openai``, and so on).  A
    non-empty default here would silently override every such fallback.
    """

    type: str = "generic"


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
      on the way out and restored on the way back. The provider's
      ``path_mapping`` can keep an upstream path prefix.

    ``routes`` maps a request path to the wire schema used to transform it.
    In transparent mode, paths not listed are forwarded verbatim when
    ``passthrough_unknown`` is set (the default) and refused with 403 when it is
    not.  The gateway serves only the listed paths either way.
    """

    mode: Literal["gateway", "transparent"] = "transparent"
    routes: dict[str, str] = Field(default_factory=_default_routes)
    passthrough_unknown: bool = True
    #: Mask each copy of a value a request masked, and refuse the request if
    #: one is still where Privyx does not mask.
    dlp_check: bool = True
    #: Developer tap: each masked exchange before and after masking, one JSONL
    #: file per session in ``tap_dir``.  It holds original values; owner-only.
    tap: bool = False
    tap_dir: str = "~/.privyx/tap"


class ModelPrice(BaseModel):
    """USD per million tokens; cache rates default to 0.1x and 1.25x of input."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)


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


class ServerConfig(BaseModel):
    """Where ``privyx proxy`` listens, and how many processes serve it."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=0, le=65535)
    #: Server processes.  More than one needs a vault they all share (``sqlite``
    #: or ``redis``); ``privyx run`` serves with one.
    workers: int = Field(default=1, ge=1)
    tls: TLSConfig = Field(default_factory=TLSConfig)


class LogConfig(BaseModel):
    """Privyx's own log: its level, its console format, and a file that also takes others'."""

    level: str = "info"
    #: ``text``, or ``json`` for one JSON object per line.
    format: str = "text"
    #: A debug log file, created owner-only.  It also takes third-party
    #: records at ``level``: at ``debug`` they hold raw vault rows.
    file: str = ""


class BudgetConfig(BaseModel):
    """Limits on one ``privyx run`` launch; unset means no limit."""

    max_turns: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentConfig(BaseModel):
    """How to launch a tool and point it at the local proxy; never a shell command.

    Every key but these is an option of the profile's agent ``type``, which
    its ``Options`` check.
    """

    model_config = ConfigDict(extra="allow")

    command: str = Field(min_length=1)
    #: The agent type that knows the tool; unset, the one the profile
    #: extends, else the type named like the profile or its command's file,
    #: else ``env``.
    type: str = ""
    provider: str = Field(default="generic", min_length=1)
    upstream: str = ""
    env_vars: list[str] = Field(default_factory=list)
    base_path: str = ""
    extends: str | None = None
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

    @field_validator("env_vars")
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


def _default_providers() -> dict[str, ProviderConfig]:
    from privyx.config.defaults import DEFAULT_PROVIDERS

    return {name: ProviderConfig.model_validate(spec) for name, spec in DEFAULT_PROVIDERS.items()}


def _default_detectors() -> dict[str, DetectorConfig]:
    return {"regex": DetectorConfig(type="regex")}


def _default_features() -> dict[str, FeatureConfig]:
    from privyx.config.defaults import DEFAULT_FEATURES

    return {
        name: FeatureConfig(type=name, enabled=enabled)
        for name, enabled in DEFAULT_FEATURES.items()
    }


class Settings(BaseModel):
    """Top-level Privyx settings.

    A key it does not define is refused, so a setting renamed in 0.3.0 and
    given here by its old name fails rather than doing nothing: the loader
    reads the old names of a file with a warning.
    """

    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    #: Where requests go when that is not the provider's own ``base_url``: a
    #: profile's ``upstream``, or the agent endpoint ``privyx run`` follows.
    #: Set by the loader, not written in a file (where ``upstream_url`` is the
    #: deprecated name of ``provider.base_url``); empty means the provider's
    #: own.  See :func:`privyx.providers.resolve_base_url`.
    upstream_url: str = ""
    vault: VaultConfig = Field(default_factory=VaultConfig)
    #: The detectors, by name, run in this order; their spans are pooled.
    detectors: dict[str, DetectorConfig] = Field(default_factory=_default_detectors)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    operator: OperatorConfig = Field(default_factory=OperatorConfig)
    anchor: AnchorConfig = Field(default_factory=AnchorConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    token: TokenConfig = Field(default_factory=TokenConfig)
    #: The provider in use: in a file, the name of one of ``providers``, or a
    #: definition of its own; the loader puts the one selected here.
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    #: The named providers, the built-in ones among them (``openai``, ``xai``, ...).
    providers: dict[str, ProviderConfig] = Field(default_factory=_default_providers)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    #: The optional features, by name, each on or off: ``audit``, ``metrics``,
    #: ``tracing``, ``directive``, ``cache_control``, a plugin's.
    features: dict[str, FeatureConfig] = Field(default_factory=_default_features)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    #: The options of each secret resolver, by scheme (``op``, a plugin's).
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=_default_agents)
    #: Named sets of settings that a ``privyx run`` profile or ``--preset`` applies.
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: ``privyx run``: also mask the workspace's git authors, remote owners and
    #: names, and ``.env`` values, read at launch and kept in memory only.
    harvest: bool = False
    #: The ``base_url`` and ``type`` the provider's credential was written
    #: with, when the loader read one: the only origin it is sent to (see
    #: :func:`privyx.providers.check_credential`).  ``None``: the provider's own.
    _home: tuple[str, str] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _named_types(self) -> Settings:
        # An entry written without a type has its name's.
        for entries in (self.detectors, self.features):
            for name, entry in entries.items():
                entry.type = entry.type or name
        return self

    @model_validator(mode="after")
    def _transparent_mapping(self) -> Settings:
        # The gateway calls a provider endpoint, not a joined path.
        if self.proxy.mode == "gateway" and self.provider.path_mapping.mode != "origin":
            raise ValueError("provider.path_mapping applies to transparent mode only")
        return self

    @property
    def active_detectors(self) -> dict[str, DetectorConfig]:
        """The detectors that are on, by name, in the order they run."""
        return {name: d for name, d in self.detectors.items() if d.enabled}

    @property
    def detector_type(self) -> str:
        """The detector types that are on, for display: ``regex``, ``regex+presidio``."""
        return "+".join(d.type for d in self.active_detectors.values()) or "none"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build Settings from a (possibly partial) config dict."""
        merged = data
        return cls(**merged)


#: Each section whose ``type`` names a component, by key: the kind of the component.
PLUGGABLE: dict[str, type[PluggableConfig]] = {
    "vault": VaultConfig,
    "policy": PolicyConfig,
    "operator": OperatorConfig,
    "anchor": AnchorConfig,
    "provider": ProviderConfig,
}

#: Each section of named entries whose type is by default the entry's name,
#: and that ``false`` turns off: its kind, and the settings of one entry.
NAMED: dict[str, tuple[str, type[PluggableConfig]]] = {
    "detectors": ("detector", DetectorConfig),
    "features": ("feature", FeatureConfig),
}
