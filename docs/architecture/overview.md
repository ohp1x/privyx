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

## Design Principles

1. **Core does not depend on FastAPI.**
2. **Core does not depend on any provider SDK.**
3. **Provider is a plugin/adapter.**
4. **Detector is a plugin.**
5. **Operator is a plugin.**
6. **Vault is a backend abstraction.**
7. **Streaming is first-class.**
8. **SSE envelope is separated from text transformation.**
9. **Session state that outlives a request must not depend on process memory.**
10. **CLI is only orchestration/UI.**
11. **Observability must not leak into privacy logic.**
12. **No plaintext PII in logs by default.**
13. **All streaming algorithms are property-tested against random chunk boundaries.**

## Module Layout

| Module | Purpose |
|---|---|
| `core/` | Engine, session, context, result, errors — transport-agnostic |
| `token/` | Logical token + configurable codec (the only place token syntax lives) |
| `privacy/` | Detectors, policies, operators, anchors, transforms |
| `streaming/` | Deanonymizer, recognizer, trie, buffer, SSE adapters |
| `vault/` | Memory, SQLite, Redis session storage |
| `providers/` | Generic, OpenAI, Anthropic transports (Google is a placeholder) |
| `proxy/` | HTTP/SSE proxy connecting engine to providers |
| `gateway/` | Optional FastAPI server |
| `cli/` | `privyx` orchestration layer |
| `config/` | Schema, loader, defaults, env |
| `security/` | Keys, crypto, secrets, redaction |
| `observability/` | Logging, metrics, tracing, audit |
| `plugins/` | Plugin registry, loader, hooks |

## Data Flow (Request)

```text
Client → HTTP Proxy → Detect → Policy → Pseudonymize → Provider
                                                    ↓
Client ← HTTP Proxy ← Restore ← Vault ←────────────────┘
```

## Data Flow (Streaming)

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

See [development/testing.md](../development/testing.md) for the test strategy.
