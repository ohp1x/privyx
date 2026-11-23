---
description: The audit trail's event schema, its guarantees, and the Prometheus metrics Privyx serves at /metrics.
---

# Audit events and metrics

Privyx appends an audit trail to `features.audit.path` (default
`~/.privyx/audit.log`), one JSON object per line. It records what happened,
never the content: event names, entity types, and counts. Its format is
versioned, so a script, a log store, or a dashboard can rely on it. A running
proxy also serves the same totals as [Prometheus metrics](#metrics).

## Envelope

Every record starts with the same envelope, followed by the fields of its
event at the same level, so a line works with `jq` and maps onto table
columns:

| field | type | notes |
|---|---|---|
| `schema_version` | int | Bumped on a backward-incompatible change. Currently `1`. Branch on it. |
| `time` | string | ISO-8601 UTC, millisecond precision (e.g. `2026-09-18T03:11:18.737Z`). Human-readable. |
| `ts` | float | Unix epoch seconds. Cheap ordering / range queries. |
| `event` | string | One of the event types below. |
| `request_id` | string \| null | Shared by every event of one proxied exchange. `null` for library and CLI calls (`mask`) outside a request. |
| `session_id` | string \| null | The session that holds the mapping, when one applies. |

## Events

Events are named `<domain>.<action>`: `session.*` events concern a session's
mapping, `proxy.*` events one HTTP exchange, and `run.*` events one
`privyx run` launch.

### `session.created`
A new session was created.

| field | type | notes |
|---|---|---|
| `source` | string | How the id was chosen: `header`, `client`, `conversation`, or `ephemeral`. |

### `session.transform`
A request was masked. A request has many pieces of text, but the trail records
one `session.transform` per exchange, with the totals.

| field | type | notes |
|---|---|---|
| `entity_counts` | object | `{entity_type: count}`: types and counts only, never the matched text. |
| `transformations` | int | Total replacements applied across the request. |
| `detector_counts` | object | Present when a detector reported work: `llm_calls`, `llm_input_tokens` / `llm_output_tokens` (estimated at ~4 characters per token), `llm_fallbacks` (scans that fell back to regex under `llm_fallback_on_error`). Counts only. |

A request whose only activity is detector work (an LLM scan that found nothing,
or a fallback) still records `session.transform`, with `transformations: 0`.

### `session.restore`
Tokens in the reply were restored. One per exchange, for batch and streamed
replies alike.

| field | type | notes |
|---|---|---|
| `transformations` | int | Tokens restored. |

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
| `method` / `path` / `schema` | string | Method, the matched route pattern (a `*` stays as written in `proxy.routes`, since the path it stands for can hold a value), and wire schema (`openai`, `anthropic`, `responses`, `embeddings`, `gemini`, `ollama`, or `null`). Unmatched paths are recorded as `[unmatched]`, never the raw URL. |
| `status` | int | Upstream HTTP status. |
| `stream` | bool | Whether the response is an SSE stream. |
| `duration_ms` | float | Time to the upstream response **headers** (not the whole stream). |
| `transform_ms` | float | The part of `duration_ms` spent masking the request (detection, pseudonymization, and the session's vault read and write); the rest is mostly the upstream. Absent when no body was masked (a path outside `proxy.routes`, a body that is not JSON). |
| `upstream` | string | Upstream host. |

### `proxy.passthrough` / `proxy.blocked`
An unknown route was allowed without body masking or refused with `403`, or
a path outside [`provider.path_mapping.strip_prefix`](../guide/configuration.md#upstream-with-a-path)
was refused with `404`.
Emitted at classification, before reading its body or contacting upstream, so
an upstream failure does not hide the decision. `body_masked: false` describes
the body; machine metadata headers can still be masked on allowed requests.

| field | type | notes |
|---|---|---|
| `method` | string | Standard HTTP method, or `OTHER` for an unrecognized method. |
| `reason` | string | `unknown_route`; `outside_prefix` for a path outside `provider.path_mapping.strip_prefix`; `no_upstream` for a `privyx run pi` request that names no base URL for its route. |
| `body_masked` | bool | Always `false`. |

No path, query, body, headers, credentials, or original values are recorded.
Use `request_id` to correlate later events. `privyx audit stats` displays the
passthrough and blocked counts; `/metrics` counts both event types.

### `proxy.response`
The response was fully delivered to the client, or the client disconnected
before a stream ended (`aborted`).

| field | type | notes |
|---|---|---|
| `status` | int | HTTP status. |
| `stream` | bool | |
| `bytes` | int | Batch responses, and streams relayed with nothing to restore (a path without a route, no mapping): response size. |
| `frames` | int | Streams restored by Privyx: SSE frames forwarded. |
| `restored` | int | Tokens restored on the way back. |
| `duration_ms` | float | **Total** exchange time. |
| `aborted` | bool | Present (`true`) when the client disconnected mid-stream, e.g. a coding tool's request cancelled with Esc; `frames` and `restored` then count what was sent. Not an error, so `privyx_proxy_errors_total` does not count it. |
| `usage` | object | Present when the upstream reported token usage: `input` (not read from the prompt cache), `output`, `cache_read`, and `cache_write`. Anthropic, OpenAI Chat Completions, OpenAI Responses, Gemini, and Ollama usage are all reported this way. |
| `model` | string | The model the upstream named in its reply, when it did. Left out when it does not look like a model id or the built-in patterns find a value in it (an email, a key, an address). |

### `proxy.error`
An exchange failed.

| field | type | notes |
|---|---|---|
| `phase` | string | Where it broke: `transform` (masking the request failed, so it was not forwarded and the client got `503`), `vault` (reading or writing the session failed before the request was sent; the client got `503`), `upstream` (no response from the upstream, or the body of a batch reply broke off after its headers; the client got `502`, `503`, or `504`, see [Errors](../reference/errors.md)), `stream`, or `response`. |
| `error_type` | string | The exception's class name, never its message. |
| `status` | int | Present when a status was already known. |
| `duration_ms` | float | Time until the failure. |
| `usage` | object | Present when a stream reported token usage before it broke, in the same shape as on `proxy.response`. |

### `run.unproxied`
A tool launched with `privyx run` exited without a single request sent
upstream on a masking route (a path in `proxy.routes`), answered or not. If it
called its provider, it did so directly, or on a path Privyx does not mask.
The terminal shows a warning only when a request with a body was passed through
unmasked, or one was blocked or failed
([When no request is masked](../guide/cli.md#when-no-request-is-masked)).

| field | type | notes |
|---|---|---|
| `profile` | string | The agent profile, or the base name of the command run without one. |
| `requests` | int | HTTP requests that reached Privyx during the launch. |
| `passthrough` | int | Requests on paths outside `proxy.routes`, forwarded without masking. |
| `blocked` | int | Requests on paths outside `proxy.routes`, refused because `proxy.passthrough_unknown` is false, or outside `provider.path_mapping.strip_prefix`. |
| `failed` | int | [`proxy.error`](#proxyerror) events. A `transform` failure was refused without forwarding; an `upstream` failure of a request passed through also counts in `passthrough`. |

### `run.budget_exceeded`
A tool launched with `privyx run` reached its profile's
[`budget`](../guide/configuration.md#agents), and Privyx refused its next model
request. Written once per launch.

| field | type | notes |
|---|---|---|
| `profile` | string | As in `run.unproxied`. |
| `limit` | string | `turn` or `token`: the limit reached; `unmeasured` when a reply came without token usage under a token limit. |
| `turns` | int | Model requests forwarded so far. |
| `tokens` | int | Tokens the upstream reported so far. |

## Guarantees

- **No content.** No field carries matched text, original values, tokens, or
  exception messages. `session.transform` takes a `{type: count}`
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
- **Best effort.** A failed audit write can never break a proxied request. The
  line is lost, the failure is logged, and
  `privyx_audit_write_failures_total` counts it. Each line is flushed to the
  operating system but not synced to disk, so a power loss can take the last
  lines with it. Do not treat the trail as a complete record of every exchange.

## Reading the trail

For the common questions, no `jq` is needed:

```bash
privyx audit stats [FILE] [--since 24h]   # totals: requests, errors, sessions, entity types, tokens, cache hit rate
privyx audit tail [FILE] [-n 20]          # one readable line per event, then follow (--no-follow)
```

`FILE` defaults to the configured `features.audit.path`.

The file is append-only JSON Lines: tail it, `jq` it, or load it into a log
store. Each line carries its `schema_version`; a reader branches on it rather
than guessing when it changes.

**Rotation.** Privyx opens `features.audit.path` once and keeps appending to that open
file, so rotate it with logrotate's `copytruncate`: a rotation that moves the
file away leaves Privyx writing to the moved file until it restarts.
`privyx audit tail` follows a truncated file from the top.

Disable the trail entirely with `features.audit: false` /
`PRIVYX_AUDIT_ENABLED=false`.

## Metrics

A running proxy serves the same totals at `GET /metrics` in the Prometheus
text format, counted in memory from startup, so they work with
`features.audit: false` and reset on restart:

| metric | labels | notes |
|---|---|---|
| `privyx_audit_events_total` | `event` | Events emitted, by event type. |
| `privyx_entities_masked_total` | `entity_type` | Sum of `session.transform` `entity_counts`. |
| `privyx_detector_counts_total` | `counter` | Sum of `session.transform` `detector_counts`. |
| `privyx_proxy_errors_total` | `phase` | `proxy.error` events by `phase`. |
| `privyx_pseudonyms_restored_total` | | Sum of `session.restore` `transformations`. |
| `privyx_response_duration_seconds` | | Summary of `proxy.response` `duration_ms`: `_sum` and `_count`, no quantiles. |
| `privyx_tokens_total` | `kind` | Sum of `proxy.response` `usage`: `input`, `output`, `cache_read`, `cache_write`. |
| `privyx_audit_write_failures_total` | | Audit lines the trail could not write. |

Each process counts its own. With `server.workers` above `1`, a scrape reads
the worker that answers it.

`/metrics` needs no key, like `/health`; it exposes counts only, but keep it off
networks where traffic volume should stay private.
