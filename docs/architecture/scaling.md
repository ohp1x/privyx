# Scaling Privyx

## Vault Backends

Session state never lives only in process memory:

| Backend | Use case |
|---|---|
| `MemoryVault` | Local dev, single process |
| `SQLiteVault` | Single server, persistence across restarts |
| `RedisVault` | Multi-instance, horizontal scaling |

`vault.ttl` (seconds) expires sessions idle that long on every backend; a
session's idle time restarts with each request that uses it.

## Multi-Instance

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

- Request/response bodies are processed in-memory; no cross-instance state.
- Session mapping lives in the vault — any instance can serve any session.
- Streaming deanonymizers are per-connection; mapping is loaded from the
  vault at stream start.
