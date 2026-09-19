# Changelog

All notable changes to Privyx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Proxy coverage

- **Privacy fix.** Many request and response fields bypassed the engine, so real
  PII reached the upstream or placeholders reached the client. Both directions
  now share one leaf walk instead of a walker per field. Restore walks *every*
  string leaf of a response (placeholders only exist because Privyx minted them).
  Transform walks the content subtrees (`messages`, `system`, `input`,
  `instructions`, `prompt`, `prediction`) and pseudonymizes every string there
  except opaque keys — ids and `*_id`, `type`, `role`, `name`, `signature`,
  `encrypted_*`, base64 `data` / `file_data`, URLs, `media_type`,
  `cache_control`, `status` — so a field added later is pseudonymized rather than
  leaked. Tool arguments are walked as parsed JSON, never as one text blob
- **OpenAI Responses API** (`/v1/responses`, `/input_tokens`, `/compact`) is now
  routed with a new `responses` schema — previously forwarded raw, though it is
  the wire API Codex CLI speaks. Requests cover `instructions`, `input` (string
  or items: messages, function / custom / MCP / shell / apply-patch calls and
  their outputs, reasoning), and prompt variables. Streams restore every string
  `delta` per item and index, buffer tool-call input until its `.done`, and
  restore the full text repeated in `*.done`, `output_item.done`, and
  `response.completed`, keeping the `event:` lines. Works in transparent and
  gateway modes
- `/v1/messages/count_tokens` is routed with the `anthropic` schema — its body is
  a Messages body and was forwarded raw
- Anthropic: `server_tool_use.input`, `document` blocks (plain-text
  `source.data`, `source.content`, `title`, `context`), `search_result`,
  `citations[].cited_text`, and code-execution `stdout` / `stderr` are now
  pseudonymized and restored; a streamed `citations_delta` is restored
- OpenAI Chat: `refusal` (field and content part), the non-standard `reasoning`
  field (OpenRouter / vLLM / Ollama), legacy `function_call.arguments`, and
  `prediction.content` are covered in requests, batch responses, and streams;
  `reasoning` and `refusal` stream on their own buffers, and `function_call` is
  buffered like `tool_calls`
- Streams restore every non-delta event (`message_start`, `content_block_start`,
  finish/usage chunks, …) leaf by leaf, re-serializing only when something
  changed
- `session.strategy: conversation` fingerprints a Responses body on the first
  user message in `input`
- Still forwarded verbatim: `/v1/embeddings`, the legacy `/v1/completions`, and
  Gemini-native paths. A gateway serving the default routes now also forwards
  `/v1/responses` to its single upstream endpoint; the token-counting and
  compaction routes stay unserved there (404) so a count never becomes a
  billed completion

### Audit

- Ephemeral proxy sessions are now deleted from the vault after a batch response
  completes or a streaming response is drained/cancelled. A new
  `session.deleted` event records the safe `mapping_count` and reason only, and
  is written only after deletion succeeds. Header-based and derived
  `client`/`conversation` sessions remain available for continuity
- Cleanup is best-effort: a vault deletion failure does not change a successful
  response or mask an existing stream/upstream error; it emits `proxy.error` with
  `phase: cleanup` and the exception class name, without a false deletion event
- The audit trail gained a **versioned, correlated envelope** so it can back a
  long-lived reader (a store, query layer, or dashboard) without reshaping. Every
  line now carries `schema_version`, a human-readable ISO-8601 `time` (next to the
  epoch `ts`), and a `request_id` that ties every event of one proxied exchange
  together. Event names are consistent `<domain>.<action>`: `transform` and
  `restore` became `session.transform` / `session.restore`, joining
  `session.created`, `proxy.request`, and the new events below. This is a
  **breaking change** to the on-disk format, signalled by `schema_version: 1`
- **Per-request aggregation.** A request pseudonymizes many text leaves; the trail
  used to write one `transform` line per leaf. It now records a single
  `session.transform` (merged `entity_counts`, summed `transformations`) and a
  single `session.restore` per exchange, so the log reads one row per event that
  matters. Standalone library / `privyx mask` calls (outside a request) still emit
  per call
- **New `proxy.response`** records the completed exchange — total duration, batch
  `bytes` or streamed `frames`, and the `restored` count — complementing
  `proxy.request` (which stays the time-to-first-byte marker). Streaming responses
  now also emit `session.restore`; previously the streaming path bypassed the
  restore accounting entirely and recorded nothing
- **New `proxy.error`** records a failed exchange with a `phase`
  (`upstream` / `stream` / `response`) and the exception's **class name** — never
  its message, which could echo payload text. The "counts, not content" guarantee
  is unchanged and now documented in `docs/observability/audit-events.md`
- Dropped the redundant `client_supplied` field from `session.created` (its
  `source` already says how the id was chosen)

### Sessions

- New `session.strategy` (`PRIVYX_SESSION_STRATEGY`) decides how a session is
  identified when the client sends no `x-privyx-session` header — which Claude
  Code, codex, aider, and the OpenAI CLI never do, so every request used to become
  a fresh throwaway session. `ephemeral` (default, the prior behavior) still mints
  one per request; `client` derives a stable session from the client credential
  (one API key → one session); `conversation` derives it from the credential and
  the first user message, so one conversation is one reused session while different
  conversations — even under the same key — stay isolated
- Continuity is automatic: the id is derived before `get_or_create_session`, which
  already reuses a vault session on a hit. So a conversation's turns share one
  session and one pseudonym map, tokens stay stable across turns, and only the
  first turn logs `session.created`. Every derived id is keyed on the credential,
  so two callers can never share a map — the sticky strategies do not reintroduce
  the cross-user collision a single shared session would cause
- `session.created` now records a `source` (`header` / `client` / `conversation` /
  `ephemeral`) next to the existing `client_supplied`, so the audit trail shows how
  each id was chosen. `get_or_create_session` also tolerates a concurrent create
  (clients fan out parallel requests at conversation start) instead of failing the
  race with a `VaultError`
- `privyx run` is opinionated where the library stays neutral: it defaults to
  `conversation` and auto-provisions a persisted per-user HMAC anchor secret
  (`~/.config/privyx/anchor.key`, honoring `$XDG_CONFIG_HOME`), so pseudonyms are
  stable across turns *and* restarts out of the box. `--session-strategy` overrides
  the strategy, `--no-anchor` skips provisioning, and a user's own
  `session.strategy` / `anchor.secret` (config or env) still wins — `load_config`
  gained a low-precedence `base_extra` channel for exactly these caller defaults
- `privyx config` and the proxy startup banner now show the active session strategy

### CLI

- `privyx proxy --reload` restarts the server when the config file changes, for
  tuning detectors and policies without a manual restart. Off by default; it is
  a restart rather than a hot swap, so in-flight requests finish and an
  in-memory vault starts empty again. A config that does not parse or does not
  build is reported and the watcher waits for the next edit instead of exiting
- A config file with invalid YAML now raises a `ConfigError` with the parser's
  message instead of surfacing a raw `yaml` traceback

- `privyx mask` / `privyx unmask` — pseudonymize and restore text without the
  proxy, for use in pipelines or from another project. Input comes from
  positional arguments, `--stdin`, or `-i FILE` (`-` for stdin); output goes to
  stdout or `-o FILE`. `-f text|json|jsonl` picks the walk (`auto` guesses from
  the input file's extension), JSON walks every string leaf and can be narrowed
  with `--path '$.messages'`, and non-string values are left untouched
- The mapping lives either in a self-contained `--map FILE` — no vault, no
  deployment needed — or in the configured vault under `--session ID`. An
  existing map file is continued rather than overwritten, so a second document
  keeps the tokens the first one was given. Because the default vault is
  `memory`, `mask` warns when neither is in play and the output could never be
  unmasked

### Detectors

- `detector.terms` takes a literal word list per entity —
  `PERSON: [ann, bob]` — instead of a hand-written regex. Privyx escapes each
  term, orders the longest first so a compound term wins over a substring of
  itself, and adds word boundaries only where the term ends in a word
  character, so a URL still matches whole. Matching is case-insensitive.
  `terms` and `patterns` are merged rather than exclusive; when both name the
  same entity the hand-written regex wins
- An invalid entity name in `detector.patterns` or `detector.terms` is now
  rejected at startup instead of raising mid-request on the first detection.
  Names follow the token codec's grammar: a letter, then letters, digits, or
  underscores

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
- A no-op `transform` or `restore` (zero replacements) is no longer recorded.
  Most of a proxied request is untouched text — system prompt, every content
  block, every tool result — and one line per skipped field buried the events
  that matter; `proxy.request` still records that the call happened

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
