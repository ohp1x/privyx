---
description: "Running Privyx as more than one process: vault backends, a shared Redis vault, and the state that stays per process."
---

# Scaling Privyx

## Vault backends

The default memory vault is limited to one process and loses sessions on
restart. Use SQLite for persistence or Redis for sessions shared across
instances. An ephemeral session lives for one request in the process serving it:

| Backend | Use case |
|---|---|
| `MemoryVault` | Local dev, single process |
| `SQLiteVault` | Single server, persistence across restarts |
| `RedisVault` | Multi-instance, horizontal scaling |

`vault.ttl` (seconds) expires sessions idle that long on every backend; a
session's idle time restarts with each request that uses it.

## More than one instance

```text
                Load Balancer
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Privyx       Privyx      Privyx
          │           │           │
          └───────────┼───────────┘
                      ▼
                    Redis
```

## Statelessness

- Request/response bodies are processed in memory. Shared session mappings
  need a shared vault; use Redis across instances.
- With Redis, any instance can load a retained session's mapping. Keep the
  same token configuration and anchor or encryption key on every instance.
- Remembered signed Anthropic thinking is process-local. Another instance
  falls back to masking the echoed text again, which can invalidate its
  signature; see [Proxy architecture](proxy.md#what-gets-transformed-and-restored).
- Without an anchor, tokens are counters, so an instance masking a request
  holds the session's Redis lock (`SET NX EX`, renewed while held) until it
  has saved the mapping. With an anchor no lock is taken: the token comes from
  the value.
- Streaming deanonymizers are per-connection; the mapping is the request's own
  (ephemeral) or loaded from the vault at stream start.
