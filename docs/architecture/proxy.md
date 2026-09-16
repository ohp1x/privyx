# Proxy Architecture

## Two serving modes

`privyx proxy` serves one of two apps, selected by `proxy.mode` (or
`--transparent` / `--gateway`):

- **`transparent`** (default) — a drop-in reverse proxy. A client points its
  `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` at Privyx and *every* path is
  forwarded to the upstream **origin**; chat paths (`/v1/chat/completions`,
  `/v1/messages`) are pseudonymized on the way out and restored on the way back,
  batch or streaming, with no client-side change. Non-chat paths (models,
  embeddings, files, …) are forwarded verbatim. "Transparent" here means
  *drop-in* — it is an HTTP reverse proxy, not a network-level MITM/SOCKS proxy.
- **`gateway`** — a narrow app exposing only `POST /v1/chat/completions`
  (OpenAI schema). Kept for chat-only deployments.

`--reload` (development only, off by default) watches the *config file* — the
one passed with `-c` or named by `PRIVYX_CONFIG`, not the source code — and
restarts the server when it changes: uvicorn shuts down gracefully, then the
engine, vault, plugins, and provider are built again from the new settings. It
is a restart, not a hot swap, so in-flight requests finish and the listener is
briefly gone, and an in-memory vault starts empty again (a `sqlite`/encrypted
vault keeps its mappings). A config that fails to parse or to build is reported
and the watcher keeps waiting, so a typo pauses the proxy instead of killing the
process.

Both modes share one implementation of request pseudonymization
(`proxy/schemas.py`), batch restore (`proxy/schemas.py`), and streaming restore
(`proxy/stream_router.py`); the transparent proxy only adds path routing,
per-request schema selection, and header/credential forwarding on top.

## Layers

The proxy is deliberately thin. It:

1. Decodes incoming HTTP requests (JSON body).
2. Hands the text to the privacy engine (pseudonymize).
3. Forwards to the upstream provider.
4. Handles the response — batch or streaming.
5. Deanonymizes before returning to the client.

## Sessions

Sessions are resolved from the `x-privyx-session` header when present, and are
otherwise **ephemeral per request**: a request is pseudonymized and its reply
restored within a single exchange, and the client only ever sees restored text,
so a fresh session per request is correct — and, unlike a single shared session,
cannot collide two concurrent callers' pseudonym maps.

## Tool calls in a stream

OpenAI `tool_calls` and Anthropic `tool_use` stream their arguments as partial
JSON across many events. Forwarding the partials would leak pseudonyms and
fragment the JSON, so `StreamRouter` accumulates them in pseudonym space and
emits each — deanonymized, as one frame — only once complete (dropping a
truncated call rather than forwarding an unusable one). Terminal events
(`[DONE]`, `message_stop`) are deferred and emitted last, after every held-back
text fragment and buffered tool call has been flushed.

## Streaming Path

```text
Provider
    ↓
SSE event
    ↓
OpenAI adapter
    ↓
text delta
    ↓
Privacy stream engine
    ↓
deanonymized delta
    ↓
OpenAI adapter
    ↓
SSE event
```

## Why SSE Envelope ≠ Text Stream

SSE events carry metadata (`event:`, `id:`, `retry:`), multi-line `data:`,
and JSON payloads that embed text at different paths (e.g.
`choices[0].delta.content` for OpenAI, `delta.text` for Anthropic).

If the proxy treated raw SSE bytes as text, it would corrupt envelopes and
transform metadata it should leave alone. The adapter layer separates
envelope handling from text transformation.
