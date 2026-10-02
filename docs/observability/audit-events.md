# Audit Events

Privyx appends a PII-safe audit trail as one JSON object per line to `audit.path`
(default `privyx-audit.log`; `~/.local/state/privyx/audit.log` for `privyx run`).
This is the stable, versioned contract a store, query layer, or dashboard reads.
Implementation: `observability/audit.py`.

## Envelope

Every record — whatever the event — starts with the same envelope, then carries
event-specific fields flattened alongside it (so the line stays `jq`-friendly and
maps cleanly onto SQL columns):

| field | type | notes |
|---|---|---|
| `schema_version` | int | Bumped on a backward-incompatible change. Currently `1`. Branch on it. |
| `time` | string | ISO-8601 UTC, millisecond precision (e.g. `2026-09-18T03:11:18.737Z`). Human-readable. |
| `ts` | float | Unix epoch seconds. Cheap ordering / range queries. |
| `event` | string | One of the event types below. |
| `request_id` | string \| null | Correlates every event of one proxied exchange. `null` for standalone library / CLI (`mask`) calls that run outside a request. |
| `session_id` | string \| null | The pseudonym-map session, when one applies. |

## Events

Naming is consistent `<domain>.<action>`: `session.*` are privacy-pipeline events
on a session; `proxy.*` describe one HTTP exchange.

### `session.created`
A new session (pseudonym map) was created.

| field | type | notes |
|---|---|---|
| `source` | string | How the id was chosen: `header`, `client`, `conversation`, or `ephemeral`. |

### `session.transform`
Request text was pseudonymized. **Aggregated**: a request pseudonymizes many text
leaves, but the trail records **one** `session.transform` per exchange.

| field | type | notes |
|---|---|---|
| `entity_counts` | object | `{entity_type: count}` histogram — types and counts only, never the matched text. |
| `transformations` | int | Total replacements applied across the request. |
| `detector_counts` | object | Present when a detector reported work: `llm_calls`, `llm_input_tokens` / `llm_output_tokens` (estimated at ~4 characters per token), `llm_fallbacks` (scans that fell back to regex under `llm_fallback_on_error`). Counts only. |

A request whose only activity is detector work (an LLM scan that found nothing,
or a fallback) still records `session.transform`, with `transformations: 0`.

### `session.restore`
Pseudonyms were reversed in the response. One per exchange, on **both** the batch
and streaming paths.

| field | type | notes |
|---|---|---|
| `transformations` | int | Total pseudonyms reversed. |

### `session.deleted`
A session and its mapping were destroyed: deleted from the vault by
`privyx session prune`, or, for the default `ephemeral` source, dropped from
memory when its exchange completed or the client disconnected. Sticky
`header`, `client`, and `conversation` sessions remain available for reuse and
therefore do not emit this event during ordinary request cleanup.

| field | type | notes |
|---|---|---|
| `reason` | string | `ephemeral_request_complete` (proxy cleanup) or `prune` (`privyx session prune`). |
| `mapping_count` | int | Number of mappings destroyed; values and keys are never recorded. |

### `proxy.request`
The upstream returned response headers (time to first byte).

| field | type | notes |
|---|---|---|
| `method` / `path` / `schema` | string | Request line and matched wire schema (`openai` / `anthropic` / `null`). |
| `status` | int | Upstream HTTP status. |
| `stream` | bool | Whether the response is an SSE stream. |
| `duration_ms` | float | Time to the upstream response **headers** (not the whole stream). |
| `transform_ms` | float | The part of `duration_ms` spent masking the request (detection, pseudonymization, and the session's vault read and write); the rest is mostly the upstream. Absent when no body was masked (a path outside `proxy.routes`, a body that is not JSON). |
| `upstream` | string | Upstream host. |

### `proxy.response`
The response was fully delivered to the client, or the client disconnected
before a stream ended (`aborted`).

| field | type | notes |
|---|---|---|
| `status` | int | HTTP status. |
| `stream` | bool | |
| `bytes` | int | Batch responses only: response size. |
| `frames` | int | Streamed responses only: SSE frames forwarded. |
| `restored` | int | Pseudonyms reversed on the way back. |
| `duration_ms` | float | **Total** exchange time. |
| `aborted` | bool | Present (`true`) when the client disconnected mid-stream, e.g. a coding tool's request cancelled with Esc; `frames` and `restored` then count what was sent. Not an error, so `privyx_proxy_errors_total` does not count it. |

### `proxy.error`
An exchange failed.

| field | type | notes |
|---|---|---|
| `phase` | string | Where it broke: `transform` (masking the request failed, so it was not forwarded and the client got `503`), `vault` (reading or writing the session failed; the client got `503`), `upstream` (no response from the upstream; the client got `502`, `503`, or `504`, see [Errors](../reference/errors.md)), `stream`, or `response`. |
| `error_type` | string | The exception's **class name** — never its message. |
| `status` | int | Present when a status was already known. |
| `duration_ms` | float | Time until the failure. |

## Guarantees

- **PII-safe by construction.** No field carries matched text, original values,
  pseudonyms, or exception messages. `session.transform` takes a `{type: count}`
  histogram, not spans; `proxy.error` takes a class name, not a message.
- **Correlated.** All events of one exchange share a `request_id`, so a reader can
  reconstruct the timeline (`session.created` → `session.transform` →
  `proxy.request` → `session.restore` → `proxy.response` → `session.deleted`, or
  `proxy.error`).
- **Lifecycle-aware.** `privyx session prune` writes `session.deleted` only
  after the vault deletion succeeds, so the trail never reports a deletion that
  failed. An ephemeral session is never in the vault; its `session.deleted`
  marks the end of its request.
- **Source-aware retention.** Default `ephemeral` sessions end with their
  exchange and never reach the vault. Explicit-header and derived `client` / `conversation` sessions stay
  in the vault until `privyx session prune` deletes them (audited) or
  `vault.ttl` expires them (not audited: the vault drops them without a caller
  to report it).
- **Resilient.** A failed audit write is logged and swallowed; it can never break
  a proxied request.

## Consuming the trail

For the common questions, no `jq` is needed:

```bash
privyx audit stats [FILE] [--since 24h]   # totals: requests, errors by phase, sessions, entity types
privyx audit tail [FILE] [-n 20]          # one readable line per event, then follow (--no-follow)
```

`FILE` defaults to the configured `audit.path`. A running proxy also serves the
same totals at `GET /metrics` in the Prometheus text format, counted in memory
from startup, so they work with `audit.enabled: false` and reset on restart:

| metric | labels | notes |
|---|---|---|
| `privyx_audit_events_total` | `event` | Events emitted, by event type. |
| `privyx_entities_masked_total` | `entity_type` | Sum of `session.transform` `entity_counts`. |
| `privyx_detector_counts_total` | `counter` | Sum of `session.transform` `detector_counts`. |
| `privyx_proxy_errors_total` | `phase` | `proxy.error` events by `phase`. |
| `privyx_pseudonyms_restored_total` | | Sum of `session.restore` `transformations`. |
| `privyx_response_duration_seconds` | | Summary (`_sum`, `_count`) of `proxy.response` `duration_ms`. |

`/metrics` needs no key, like `/health`; it exposes counts only, but keep it off
networks where traffic volume should stay private.

**Rotation.** Privyx opens `audit.path` once and keeps appending to that open
file, so rotate it with logrotate's `copytruncate`: a rotation that moves the
file away leaves Privyx writing to the moved file until it restarts.
`privyx audit tail` follows a truncated file from the top.

The file is append-only JSON Lines: tail it, `jq` it, or bulk-load it. Because
each line is self-describing (versioned envelope, flat fields) it maps directly to
a row in a future audit store / dashboard without reshaping. When
`schema_version` changes, a reader branches on it rather than guessing.

Disable the trail entirely with `audit.enabled: false` /
`PRIVYX_AUDIT_ENABLED=false`.
