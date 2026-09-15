# Changelog

All notable changes to Privyx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
