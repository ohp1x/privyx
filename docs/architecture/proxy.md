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
- **`gateway`** — Privyx's *own* endpoints, all forwarded to the single upstream
  endpoint from `provider.base_url` (`resolve_base_url`), used **verbatim**: the
  incoming path is not echoed upstream. Which paths are served, and the wire
  schema each speaks, comes from the same `proxy.routes` map
  (`/v1/chat/completions` → openai, `/v1/messages` → anthropic by default).

Pick `gateway` when the upstream endpoint carries a **base path** —
`https://api.deepseek.com/anthropic/v1/messages`, an Azure deployment path, an
OpenRouter prefix. Transparent mode cannot express one: `resolve_origin` strips
`base_url` back to `scheme://host` and appends the client's path. Pick
`transparent` when the client's paths must be mirrored, or when non-chat paths
(models, embeddings, files) have to reach the upstream too.

Neither mode translates between wire schemas. Privyx only knows *where the text
leaves live* in each one, so an Anthropic client still needs an Anthropic-speaking
upstream.

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
per-request schema selection, and header/credential forwarding on top. The
`gateway/` package holds nothing but the two FastAPI apps — `server.py` and
`transparent.py` — since all privacy logic lives in `proxy/`.

## Layers

The proxy is deliberately thin. It:

1. Decodes incoming HTTP requests (JSON body).
2. Hands the text to the privacy engine (pseudonymize).
3. Forwards to the upstream provider.
4. Handles the response — batch or streaming.
5. Deanonymizes before returning to the client.

## Sessions

A session owns the token→value mapping used to restore a response. An explicit
`x-privyx-session` header always picks the session. Most clients (Claude Code,
codex, aider, the OpenAI CLI) never send one, so `session.strategy` decides the
fallback (`proxy/session.py::resolve_session_id`, applied by both the transparent
proxy and the gateway before `get_or_create_session`):

- **`ephemeral`** (default) — a fresh session per request. Pseudonymize and
  restore happen within one exchange and the client only ever sees restored text,
  so a per-request session is correct and — unlike a single shared session —
  cannot collide two callers' pseudonym maps. The cost is that a multi-turn
  conversation is re-detected every turn and shows up as *many* sessions, and
  (without an anchor) its tokens carry no meaning across turns.
- **`client`** — a stable session derived from the client credential
  (`Authorization` / `x-api-key`): one API key → one reused session. Simple, but
  every conversation under that key shares one growing pseudonym map.
- **`conversation`** — derived from the credential *and* the first user message,
  so one conversation → one reused session while different conversations (even
  under the same key) stay isolated. This is what `privyx run` uses.

Because the id is derived from the request, continuity is automatic:
`get_or_create_session` reuses the vault session whenever the id already exists,
so tokens stay stable across turns and only the first turn logs `session.created`.
Keying every derived id on the credential means two callers can never share a map
— the cross-user collision a single shared session would cause. The
`session.created` audit event records a `source` (`header` / `client` /
`conversation` / `ephemeral`) so the trail shows how each id was chosen.

**Trade-off of `conversation`:** the fingerprint is the *first user message*, which
is immutable across a conversation's turns. Two conversations under the same key
that open with an identical first message therefore merge, and editing or clearing
the first message starts a new session — usually what you want, but a heuristic, so
prefer an explicit `x-privyx-session` header when exact isolation matters.

For cross-turn pseudonym stability *without* continuity — or across restarts and
across conversations — set an HMAC anchor secret (`PRIVYX_ANCHOR_SECRET`), which
makes a token value-derived rather than a per-session counter. `privyx run`
provisions one automatically.

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
