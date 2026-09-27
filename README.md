# Privyx

[![PyPI](https://img.shields.io/pypi/v/privyx)](https://pypi.org/project/privyx/)
[![Python](https://img.shields.io/pypi/pyversions/privyx)](https://pypi.org/project/privyx/)
[![Docker Pulls](https://img.shields.io/docker/pulls/ohp1x/privyx)](https://hub.docker.com/r/ohp1x/privyx)
[![License](https://img.shields.io/pypi/l/privyx)](https://pypi.org/project/privyx/)

> AI data privacy gateway — a privacy engine + proxy for LLM providers.

Privyx is a modular, extensible privacy gateway that intercepts traffic to/from AI
providers, pseudonymizes sensitive data (PII), manages session vaults, and
deanonymizes streaming responses — all without changing your application code.

## Features

- **Privacy Engine** — pluggable detectors, policies, operators, and anchors
- **Transparent Proxy** — works with OpenAI, Anthropic, and any OpenAI-compatible HTTP provider
- **Native TLS/HTTPS** — the proxy can terminate TLS directly, no reverse proxy required
- **Streaming Deanonymization** — real-time pseudonym reversal in SSE streams
- **Session Vault** — memory, SQLite, or Redis backends
- **Docker-ready** — multi-stage `Dockerfile` and `docker-compose.yml` with a Redis vault
- **CLI** — `privyx proxy`, `privyx run`, `privyx detect`, `privyx mask`/`unmask`,
  `privyx session`, `privyx audit`, `privyx doctor`
- **Plugin System** — custom detectors, operators, and providers

## Quick Start

```bash
# Install
uv sync --all-extras

# Start the drop-in transparent proxy (default mode)
privyx proxy --upstream https://api.openai.com

# Run a provider through the proxy
privyx run openai
```

Then point any client at Privyx — no code change, just the base URL:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1      # OpenAI clients
export ANTHROPIC_BASE_URL=http://localhost:8000      # Anthropic clients
```

Chat Completions, Anthropic Messages, and OpenAI Responses requests (plus their
token-count endpoints) are pseudonymized before forwarding and the reply is
restored on the way back (batch or streaming, including tool-call arguments);
every other path is forwarded verbatim.

Use `--gateway` when the upstream endpoint carries a base path (say
`https://api.deepseek.com/anthropic/v1/messages`): that mode serves Privyx's own
endpoints and posts to `provider.base_url` verbatim, instead of appending the
client's path to a bare origin. See [docs/architecture/proxy.md](docs/architecture/proxy.md).

### TLS / HTTPS

The proxy can terminate TLS natively instead of sitting behind a separate reverse
proxy:

```bash
privyx proxy --upstream https://api.openai.com \
  --ssl-certfile cert.pem --ssl-keyfile key.pem
```

Equivalent config via `tls:` in `config.yaml`, or the `PRIVYX_SSL_*` /
`PRIVYX_TLS_*` environment variables. When configured, both transparent and
gateway proxy modes listen on `https://`.

### Docker

```bash
cp .env.example .env   # fill in upstream keys and secrets
docker compose up -d
```

This starts Privyx alongside a Redis-backed session vault (see
`docker-compose.yml`). Configs and plugins are mounted read-only from
`./configs` and `./plugins`; persistent data (vault DB, audit log) lives in the
`privyx_data` volume. Run the published image standalone with:

```bash
docker run -p 127.0.0.1:8000:8000 --env-file .env ohp1x/privyx:latest
```

Images are pushed to Docker Hub (`ohp1x/privyx`) and GHCR on every release tag
(`X.Y.Z`, `X.Y`, and `latest` for stable releases); see
[docs/docker.md](docs/docker.md) for the variables and volumes the image uses.
To build locally instead: `docker build -t privyx .`

### Sessions

Most clients (Claude Code, codex, aider) never send an `x-privyx-session` header,
so by default each request is its own **ephemeral** session — safe, but a
multi-turn conversation is re-tokenized every turn and shows up as many sessions.
Set `session.strategy` (or `PRIVYX_SESSION_STRATEGY`) to make sessions stick:

- `client` — one session per API key;
- `conversation` — one session per conversation, keyed on the first user message,
  so a conversation's turns share a pseudonym map while different conversations
  (even under the same key) stay isolated.

`privyx run` uses `conversation` and auto-provisions an HMAC anchor secret
(`~/.config/privyx/anchor.key`), so pseudonyms are stable across turns and
restarts out of the box. See
[docs/architecture/proxy.md](docs/architecture/proxy.md#sessions).

Sticky sessions stay in the vault until something removes them. Set
`vault.ttl` (seconds) to expire sessions idle that long, or clean up by hand:

```bash
privyx session list                        # ids, last activity, mapping counts
privyx session show ses_1234 [--reveal]    # one session's mappings, masked by default
privyx session prune --older-than 7d       # add --dry-run to preview
```

### Audit trail and metrics

Every exchange is recorded in `audit.path` (default `privyx-audit.log`) as
PII-safe JSON lines: event names, entity types, and counts, never content
([docs/observability/audit-events.md](docs/observability/audit-events.md)).

```bash
privyx audit stats [--since 24h]           # requests, errors, sessions, masked entity types
privyx audit tail                          # last events, then follow new ones (--no-follow)
curl localhost:8000/metrics                # the same counters, Prometheus text format
```

### Pseudonymizing your own terms

Beyond the built-in patterns, `detector.terms` takes plain word lists — Privyx
escapes them and matches case-insensitively, longest term first:

```yaml
# my.yaml
detector:
  type: regex                 # keeps the built-in EMAIL/PHONE/CREDIT_CARD/IP_ADDRESS/SSN
  terms:
    PERSON:       [ann, bob]
    ORGANIZATION: [acme, initech]
    URL:          ["https://git.internal.example/team"]
```

```bash
privyx detect -c my.yaml --transform "ann at acme"   # → <PRIVYX_PERSON_1> at <PRIVYX_ORGANIZATION_2>
privyx proxy -c my.yaml --upstream https://api.openai.com

# While tuning the list: --reload restarts the proxy whenever my.yaml changes
privyx proxy -c my.yaml --reload --upstream https://api.openai.com
```

The entity name is yours to choose — it becomes the `{type}` in the token.
`terms` and `patterns` can both be set; see `configs/examples/terms.yaml`.
A `patterns` regex with a `(?P<value>...)` group masks only that group —
`PASSWORD=(?P<value>\S+)` hides the secret, not the variable name.

To run several detectors at once, give `detector` a list. Each item is its own
detector config, and their spans are pooled:

```yaml
detector:
  - type: regex               # structured PII + your terms
    terms:
      PROJECT: [bluebird]
  - type: presidio            # names, orgs, locations (needs privyx[presidio])
    language: en
  - type: llm                 # context-dependent PII an LLM can spot (needs privyx[providers])
    llm_provider: openai      # or anthropic; api key defaults to the SDK's own env var
    llm_timeout: 30           # seconds per scan; a failure fails the request (fail-closed)
    llm_max_chars: 4000       # longer text is scanned in overlapping chunks, never truncated
    llm_fallback_on_error: false
```

If the LLM detector fails, times out, or replies without a JSON array of spans
(it chatted, or ran out of tokens), the request fails with `503`
(`privyx_scan_failed`) rather than reach the upstream with a weaker scan. `llm_fallback_on_error: true` keeps traffic flowing
by scanning with the built-in regex patterns instead, which lets through
whatever only the LLM would have caught (names, organizations). Each fallback is
logged and counted in the audit trail as `detector_counts.llm_fallbacks`,
alongside `llm_calls` and estimated `llm_input_tokens` / `llm_output_tokens`.

Detection results are cached in an LRU cache (`detector.cache: true`, default) so that
in multi-turn conversations, unchanged previous turns do not need to be scanned again.
Disable with `cache: false` or `PRIVYX_DETECTOR_CACHE=false`.

### Masking without the proxy

`mask` and `unmask` run the same engine over a string, a file, JSON, JSONL, or
stdin — handy in a pipeline, in CI, or from another project:

```bash
privyx mask --map map.json "mail alice@example.com"   # -> mail <PRIVYX_EMAIL_1>
privyx unmask --map map.json -i masked.txt            # -> mail alice@example.com

# JSON: every string leaf, or just one subtree
privyx mask --map map.json -i request.json
privyx mask --map map.json -i request.json --path '$.messages'
cat events.jsonl | privyx mask --map map.json -f jsonl -i - -o masked.jsonl
```

The `--map` file holds the token mapping and is enough to reverse the output
anywhere. Use `--session ID` instead to keep the mapping in the configured
vault — that needs a persistent one (`vault.type: sqlite` or `redis`), since
the default `memory` vault forgets it the moment the command exits.

## Installation

```bash
pip install privyx
# or
uv add privyx
```

Optional extras: `sqlite`, `redis`, `providers` (LLM detector), `presidio`, `faker`, `crypto`.
See [Getting started](https://ohp1x.github.io/privyx/guide/getting-started/).

## Documentation

The documentation site at [ohp1x.github.io/privyx](https://ohp1x.github.io/privyx/)
covers architecture, provider guides, and security. Its source is in [docs/](docs/).

## License

MIT
