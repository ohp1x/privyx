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
- A save writes the whole session, so an instance masking a request of a
  stored session holds that session's lock (in Redis, `SET NX EX`, renewed
  while held) until it has saved the mapping, with or without an anchor.
  Requests of one session take turns; one that waits more than 10 s gets a
  `503`. A holder that lost the lock before saving is refused and answers
  `503`, so it cannot overwrite the next holder's mapping. An ephemeral session
  is never stored and takes no lock.
- Streaming deanonymizers are per-connection. The mapping comes from the session
  the request was masked with, held in memory, so no vault is read once the
  request has gone upstream.

## Workers

Run one worker per instance, and add instances to scale. `server.workers` is an
option for a gateway bound by CPU, on one host: in a benchmark of independent
sessions, 2 workers served 1.56 times the requests of one, and 4 workers 2.23
times. It needs a vault the workers share, and each worker counts its own
[metrics](../guide/deployment.md#logs-audit-trail-and-metrics).

Restoring from the mapping held in memory has a cost: a batch request waiting
for the upstream on a session of 10,000 entries holds about 1.7 MB.
