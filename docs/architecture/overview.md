---
description: How Privyx's privacy engine, proxy, vault, and providers fit together, the design principles behind them, and the module layout.
---

# Architecture overview

Privyx is a privacy engine with an HTTP proxy around it. This page shows how
the parts fit together. For what the proxy does from a user's point of view,
see [How it works](../guide/how-it-works.md).

## Architecture

```text
┌────────────────────────────────────────────────────────┐
│                      PRIVYX GATEWAY                     │
│                                                        │
│  HTTP ──┐                                              │
│  SSE ───┴──► Protocol / Provider Adapter               │
│                         │                              │
│                         ▼                              │
│                  Privacy Pipeline                      │
│                         │                              │
│        ┌────────────────┼────────────────┐             │
│        ▼                ▼                ▼             │
│     Detector          Policy          Operator         │
│        │                │                │             │
│        └────────────────┼────────────────┘             │
│                         ▼                              │
│                     Anchor                             │
│                         │                              │
│                         ▼                              │
│                       Vault                            │
│                         │                              │
│            Memory / SQLite / Redis                     │
└─────────────────────────┬──────────────────────────────┘
                          │
                          ▼
               ┌────────────────────┐
               │    AI Providers    │
               │ OpenAI Anthropic   │
               │ Gemini DeepSeek... │
               └────────────────────┘
```

## Design principles

1. **Core does not depend on FastAPI.**
2. **Core does not depend on any provider SDK.**
3. **Provider is a plugin/adapter.**
4. **Detector is a plugin.**
5. **Operator is a plugin.**
6. **Vault is a backend abstraction.**
7. **Streaming is first-class.**
8. **SSE envelope is separated from text transformation.**
9. **Sessions shared across instances need a shared vault; durable sessions need persistent storage.**
10. **CLI is only orchestration/UI.**
11. **Observability must not leak into privacy logic.**
12. **No plaintext PII in logs by default.**
13. **All streaming algorithms are property-tested against random chunk boundaries.**

## Packages and layers

Each package under `src/privyx/` sits on a layer and imports only its own layer
and those below:

| Layer | Package | Holds | May import |
|---|---|---|---|
| 0 | `core/` | Components, registries, the event bus, exchange, session, results, errors | No other package |
| 1 | `events/` | The public event catalog | Layers 0-1 |
| 1 | `token/` | Logical token and configurable codec, the only place token syntax lives | Layers 0-1 |
| 1 | `security/` | Keys, crypto, redaction | Layers 0-1 |
| 1 | `streaming/` | SSE parsing, hold-back restore, stream router | Layers 0-1 |
| 2 | `config/` | Settings, files and layers, presets, environment, migration | Layers 0-2 |
| 3 | `detectors/`, `policies/`, `operators/`, `anchors/`, `vaults/`, `providers/`, `secrets/`, `dialects/`, `agents/`, `features/` | The kinds of component, each with its built-in types | Layers 0-2, and no other kind |
| 4 | `engine/` | `PrivacyEngine`: detect, apply policy, operate, store; no HTTP | Layers 0-4 |
| 5 | `proxy/` | The exchange pipeline, routing, headers, leak check, upstream, and the ASGI adapter, the one place that knows FastAPI | Layers 0-5 |
| 6 | `workspace/` | Scan, harvest, git hook | Layers 0-6 |
| 7 | `app/` | `Privyx`: registries, plugins, services, lifecycle, serving, `privyx run` | Layers 0-7 |
| 8 | `cli/`, `mcp/`, `testing/` | The `privyx` command, the MCP server, conformance suites | Everything |

`observability/` holds the audit logger, logging setup, tracing helpers, and
usage counting. Each of its modules counts at the layer of the package it
serves: the audit logger with `events`, tracing with `features`, usage with
`dialects`, and logging setup with `app`.

`tests/unit/test_layers.py` enforces the map. An import inside a function
counts; one under `if TYPE_CHECKING:` does not. A module whose name starts with
`_` is private to its package: other packages import what the package's
`__init__` exports, except `app`, which assembles them all. The one allowed
exception is usage counting, which asks `detectors` to tell a model name from
personal data.

## Data flow (request)

```text
Client → HTTP Proxy → Detect → Policy → Pseudonymize → Provider
                                                    ↓
Client ← HTTP Proxy ← Restore ← Vault ←────────────────┘
```

## Data flow (streaming)

```text
Provider → SSE event → Adapter → text delta → Privacy Stream Engine
                                          (codec recognizer + hold-back)
Client ← SSE event ← Adapter ← deanonymized delta
```

The hold-back scan guarantees streamed output equals batch output, even when a
token is split across chunk boundaries.

## Development

```bash
uv sync --all-extras
make test       # or: uv run pytest
make lint       # or: uv run ruff check src tests
```

See [Testing](../development/testing.md) for the test strategy.
