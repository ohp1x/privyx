# Audit Events

Privyx appends a PII-safe audit trail as one JSON object per line to `audit.path`
(default `privyx-audit.log`). This is the stable, versioned contract a store,
query layer, or dashboard reads. Implementation: `observability/audit.py`.

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

### `session.restore`
Pseudonyms were reversed in the response. One per exchange, on **both** the batch
and streaming paths.

| field | type | notes |
|---|---|---|
| `transformations` | int | Total pseudonyms reversed. |

### `session.deleted`
A session was successfully deleted from the vault. For the default `ephemeral`
source, this follows exchange completion (or stream cancellation). Sticky
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
| `upstream` | string | Upstream host. |

### `proxy.response`
The response was fully delivered to the client.

| field | type | notes |
|---|---|---|
| `status` | int | HTTP status. |
| `stream` | bool | |
| `bytes` | int | Batch responses only: response size. |
| `frames` | int | Streamed responses only: SSE frames forwarded. |
| `restored` | int | Pseudonyms reversed on the way back. |
| `duration_ms` | float | **Total** exchange time. |

### `proxy.error`
An exchange failed.

| field | type | notes |
|---|---|---|
| `phase` | string | Where it broke: `upstream`, `stream`, `response`, or `cleanup`. |
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
- **Lifecycle-aware.** `session.deleted` is written only after the vault deletion
  succeeds. A cleanup failure produces `proxy.error` with `phase: "cleanup"` and
  leaves the session available; it never produces a false deletion event.
- **Source-aware retention.** Default `ephemeral` sessions are removed after the
  exchange. Explicit-header and derived `client` / `conversation` sessions stay
  in the vault until `privyx session prune` deletes them (audited) or
  `vault.ttl` expires them (not audited: the vault drops them without a caller
  to report it).
- **Resilient.** A failed audit write is logged and swallowed; it can never break
  a proxied request.

## Consuming the trail

The file is append-only JSON Lines: tail it, `jq` it, or bulk-load it. Because
each line is self-describing (versioned envelope, flat fields) it maps directly to
a row in a future audit store / dashboard without reshaping. When
`schema_version` changes, a reader branches on it rather than guessing.

Disable the trail entirely with `audit.enabled: false` /
`PRIVYX_AUDIT_ENABLED=false`.
