# Changelog

All notable changes to Privyx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Detectors

- `detector.type: presidio` is now selectable. The `PresidioDetector` class
  existed and its docstring told you to enable it via config, but
  `build_detector` only knew `regex`, `yaml`, and plugins — so that config
  failed with `unknown detector type: presidio`
- Presidio entity names are translated to the vocabulary the rest of Privyx
  speaks (`EMAIL_ADDRESS` → `EMAIL`, `PHONE_NUMBER` → `PHONE`, `US_SSN` → `SSN`,
  …). Without this, `policy: strict` would drop every Presidio span — its
  allow-list holds `EMAIL`, not `EMAIL_ADDRESS` — and forward the PII untouched.
  Unlisted types keep their Presidio name
- The spaCy NLP engine is configured explicitly (`detector.model`, default
  `{language}_core_web_sm`) instead of relying on Presidio's default, which
  quietly expects `en_core_web_lg` to be installed
- New config: `detector.language`, `detector.model`, `detector.entities`
  (empty → every recognizer), `detector.score_threshold` (default `0.35`;
  Presidio scores NER hits well below 1.0)
- `presidio` is an optional extra (`pip install privyx[presidio]`); a missing
  package or a missing spaCy model fails at startup with a `ConfigError`, never
  mid-request, matching the `faker` and `encrypt` contract. Analyzers are cached
  per `(language, model)` so repeated builds do not reload spaCy
- The `llm` detector remains unregistered

### Operators

- New `FakerOperator` (`operator.type: faker`): replaces each detected span with a
  *realistic* fake of the same entity type (fake email, name, phone, SSN, …)
  instead of a token, so a model can reason over plausibly-shaped data. Reversible
  through the session vault; the same value fakes identically within a session and
  — via `operator.seed` — across runs, while `operator.locale` selects the Faker
  locale
- Restoration is **literal**: a fake value is ordinary text with no delimiter, so
  reversal matches the exact substituted strings (leftmost, longest-match) rather
  than the token codec. Batch and streaming share that matching, so the
  `stream == batch` property still holds — `StreamRouter` now takes an injectable
  processor factory, and the transparent proxy and gateway pick the trie
  recognizer for a literal-restore operator (`stream_restore = "literal"`) and the
  codec recognizer otherwise
- Documented trade-off: a fake value that also appears naturally in a response can
  be restored by coincidence — unlike the syntactically distinctive `<PRIVYX_…>`
  tokens. Prefer `pseudonym` when collision-free reversal matters more than realism
- `faker` is an optional extra (`pip install privyx[faker]`); selecting it without
  the package fails at startup with a `ConfigError`, never mid-request
- New `EncryptOperator` (`operator.type: encrypt`): the last operator, and the
  first that keeps **no plaintext at rest**. It stores the AES-256-GCM
  *ciphertext* of each value in the session vault (not the original) and decrypts
  on restore, closing the at-rest gap `docs/security/cryptography.md` called out.
  Encryption is deterministic (SIV-style nonce derived from the value), so the
  same value maps to the same token — dedup and idempotent writes — while the GCM
  tag means a corrupted or foreign token is passed through untouched rather than
  restored to garbage
- The token is an ordinary codec token (keyed-HMAC identifier), so streaming reuses
  the codec recognizer: batch and streaming share one decrypting resolver, and the
  `stream == batch` property is preserved and property-tested. A new optional
  `resolve` hook on the shared `restore` helper (and `Operator.build_resolver`)
  is the seam — no operator hard-codes how the vault value maps back to plaintext
- `encrypt` is an optional extra (`pip install privyx[crypto]`) and needs a key
  (`operator.key` / `PRIVYX_ENCRYPT_KEY`, 64 hex chars); a missing package, a
  missing key, or a malformed key fails at startup, never mid-request. No operator
  remains a placeholder now

### Plugins

- Added a local, opt-in plugin loader configured with `plugins.paths` (or
  `PRIVYX_PLUGIN_PATHS`); configured files and directories are imported without
  third-party entry points or `sys.path` mutation
- Concrete subclasses of `BaseDetector`, `BaseOperator`, `BasePolicy`,
  `BaseProvider`, `BaseAnchor`, and `BaseVault` are auto-discovered and
  registered by their class-level `name`; optional `from_config` factories are
  supported
- Added startup and shutdown lifecycle hooks, duplicate-name detection, and
  fail-fast `ConfigError` handling for missing paths, import failures, and
  startup-hook failures
- Plugin types are available throughout the builders and CLI commands, while
  built-in types always take precedence. `privyx doctor` reports loaded plugin
  families and names
- Added the `plugins/detectors/license_plate.py` example, plugin documentation,
  and 17 loader/registry tests. Vault plugin types are now accepted by config

### Observability

- Application logging is now wired in: `configure_logging` runs when `privyx
  proxy` starts, and its JSON formatter serializes with `json.dumps` (the old
  placeholder embedded `%(message)s` in a JSON template, so any quote or newline
  corrupted the line). Re-configuring is idempotent
- New PII-safe audit trail (`observability/audit.py`): `AuditLogger` appends one
  JSON object per line — `session.created`, `transform`, `restore`,
  `proxy.request` — to a dedicated file (`audit.path`, default
  `privyx-audit.log`). It records entity **types and counts** and request
  metadata, never payload content, original values, or pseudonyms — enforced at
  the API (the `transform` helper takes a histogram, not spans)
- Audit events are emitted transport-agnostically: the engine reports privacy
  events, the transparent proxy and gateway report `proxy.request`. The logger
  is injected (disabled no-op by default), so `privyx doctor` and unit tests
  neither open nor write a file
- Config: `audit.enabled` / `audit.path` (`PRIVYX_AUDIT_ENABLED`,
  `PRIVYX_AUDIT_PATH`). Writes are resilient — a failed write is logged and
  swallowed, never breaking a request

### Token system

- New `token/` subsystem: a `LogicalToken` (namespace/type/identifier) plus a
  configurable `FormatCodec` that is the single source of truth for how tokens
  look in text (`token/model.py`, `token/codec.py`)
- Token syntax is now configuration (`token.format` / `token.namespace`,
  `PRIVYX_TOKEN_FORMAT`, `PRIVYX_TOKEN_NAMESPACE`). The default reproduces the
  existing `<PRIVYX_EMAIL_1>` output; alternatives such as
  `[[{namespace}:{type}:{id}]]` need no code change
- Operators build logical tokens and delegate serialization to the codec;
  `restore` locates tokens via the codec. No operator, the proxy, or storage
  hard-codes token syntax anymore
- Streaming reversal is codec-driven (`streaming/recognizer.py`,
  `TokenStreamProcessor`): tokens split across arbitrary chunk boundaries are
  reconstructed as logical tokens, with `stream == batch` property-tested
- `HashOperator` now emits the one configured token syntax instead of its own
  `HASH_…` placeholder
- Removed the unused `streaming/frontier.py`

## [0.1.0] - 2026-09-13

First release: the privacy pipeline, the streaming proxy, and the CLI that
drives them. Every component named in a config is pluggable through a registry,
and the core stays free of FastAPI and provider SDKs.

### Privacy engine

- `PrivacyEngine` orchestrating detect → policy → operate over a session vault
  (`core/engine.py`, `core/session.py`, `core/context.py`, `core/result.py`)
- `core/builder.py` assembles an engine from validated settings, so every knob
  in `configs/*.yaml` reaches a real component
- Detectors: `regex` (layered over the built-in patterns) and `yaml` (exactly
  the patterns you list); Presidio and LLM detectors are placeholders
- Policies: `default` and `strict` (allow-list)
- Operators: `pseudonym`, `redact`, `hash`; `encrypt` and `faker` are
  deliberately unregistered so a config cannot select an unimplemented one
- Anchors: `hmac` makes pseudonyms deterministic across sessions — the same
  value gets the same pseudonym under the same key. An empty `anchor.secret`
  means no anchoring rather than anchoring with a guessable key. PASP is a
  placeholder

### Streaming

- `StreamingDeanonymizer`: trie plus hold-back buffer, so a pseudonym split
  across chunk boundaries is restored before the client sees any of it
- Stateful `SSEDecoder` alongside the stateless `parse_sse`
- Stream adapters for OpenAI and Anthropic envelopes, selected by name, with
  plain SSE as the fallback for custom endpoints
- SSE envelope handling is kept separate from text transformation

### Transport

- Vaults: `memory`, `sqlite` (optional extra), `redis` (optional extra) behind
  one `Vault` protocol — no pipeline state lives in process memory
- Providers: `generic`, `openai`, `anthropic`, resolved through a registry
- `HTTPProxy` connecting the engine to any provider; FastAPI gateway ships as
  the optional `[server]` extra

### CLI

- `privyx proxy` — run the gateway
- `privyx run <tool>` — start a proxy on a free port and launch `claude`,
  `codex`, `openai`, or `aider` pointed at it
- `privyx detect` — inspect what the *configured* detector and policy see
- `privyx inspect session` — show a session's mapping, masked unless `--reveal`
- `privyx doctor` — six checks, each exercising the configured component
- `privyx config` — show the resolved configuration

### Configuration

- Precedence: built-in defaults < YAML file < environment
- Upstream resolution: `upstream_url` < `provider.base_url` < the provider
  type's documented default
- Shipped configs: `default`, `strict`, and examples for OpenAI, Anthropic, and
  a custom endpoint

### Tests

130 tests: unit, integration, and Hypothesis property tests. The streaming
properties compare against the batch operator rather than the streaming path
itself, and run over both pseudonym suffix styles — counters and anchor tokens.
