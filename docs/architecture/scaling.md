# Scaling Privyx

## Vault Backends

Session state that outlives a request never lives only in process memory (an
ephemeral session lives for one request, in the memory of the process serving
it):

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
- A sticky session's mapping lives in the vault — any instance can serve any
  session.
- Without an anchor, tokens are counters, so an instance masking a request
  holds the session's Redis lock (`SET NX EX`, renewed while held) until it
  has saved the mapping. With an anchor no lock is taken: the token comes from
  the value.
- Streaming deanonymizers are per-connection; the mapping is the request's own
  (ephemeral) or loaded from the vault at stream start.
