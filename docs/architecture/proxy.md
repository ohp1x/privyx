# Proxy Architecture

## Two serving modes

`privyx proxy` serves one of two apps, selected by `proxy.mode` (or
`--transparent` / `--gateway`):

- **`transparent`** (default) — a drop-in reverse proxy. A client points its
  `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` at Privyx and *every* path is
  forwarded to the upstream **origin**; the routed paths (see
  [Routes](#routes)) are pseudonymized on the way out and restored on the way
  back, batch or streaming, with no client-side change. Every other path
  (models, embeddings, files, …) is forwarded verbatim. "Transparent" here means
  *drop-in* — it is an HTTP reverse proxy, not a network-level MITM/SOCKS proxy.
- **`gateway`** — Privyx's *own* endpoints, all forwarded to the single upstream
  endpoint from `provider.base_url` (`resolve_base_url`), used **verbatim**: the
  incoming path is not echoed upstream. Which paths are served, and the wire
  schema each speaks, comes from the same `proxy.routes` map (see
  [Routes](#routes)). Every served path posts to that one endpoint, so the
  token-counting and compaction routes (`/v1/messages/count_tokens`,
  `/v1/responses/input_tokens`, `/v1/responses/compact`) are left unserved —
  forwarding them would turn a count into a billed completion — and a default
  gateway answers `/v1/responses` by forwarding it there too; narrow
  `proxy.routes` to the paths your upstream endpoint actually speaks.

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

## HTTPS & TLS Termination

Privyx supports HTTPS via two methods:

### 1. Built-in Native TLS (Uvicorn)

Pass the certificate and private key via CLI options:

```bash
privyx proxy --ssl-certfile /path/to/cert.pem --ssl-keyfile /path/to/key.pem
```

Or configure via `config.yaml`:

```yaml
tls:
  certfile: /path/to/cert.pem
  keyfile: /path/to/key.pem
  keyfile_password: ""  # optional password if key is encrypted
  ca_certs: ""          # optional CA bundle
```

Or via environment variables:

```bash
export PRIVYX_SSL_CERTFILE=/path/to/cert.pem
export PRIVYX_SSL_KEYFILE=/path/to/key.pem
```

When both files are configured, the proxy serves HTTPS (`https://host:port`) directly.

### 2. Reverse Proxy (Production Recommended)

In production deployments, you can place a dedicated reverse proxy (e.g. Caddy, Nginx, Traefik, or Cloudflare Tunnel) in front of Privyx:

**Caddy Example (`Caddyfile`):**

```caddy
api.privacy.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy handles automatic TLS certificate issuance and renewal via Let's Encrypt.

**Nginx Example:**

```nginx
server {
    listen 443 ssl http2;
    server_name api.privacy.example.com;

    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Support Server-Sent Events (SSE) streaming:
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

## Routes

`proxy.routes` maps a request path (matched without its query string, so
`/v1/messages?beta=true` routes too) to the wire schema used to walk its body.
The default:

| Path | Schema |
|---|---|
| `/v1/chat/completions` | `openai` |
| `/v1/messages` | `anthropic` |
| `/v1/messages/count_tokens` | `anthropic` |
| `/v1/responses` | `responses` |
| `/v1/responses/input_tokens` | `responses` |
| `/v1/responses/compact` | `responses` |

Not covered — forwarded verbatim, so any PII in them reaches the upstream:
`/v1/embeddings`, the legacy `/v1/completions`, Gemini's native
`generateContent`, and reads of stored objects such as `GET /v1/responses/{id}`.
Add a route only for a path whose body really has one of the shapes above.

To close that gap, set `proxy.passthrough_unknown: false`: the transparent proxy
then answers every unrouted path with a 403 instead of forwarding it, including
harmless ones such as `GET /v1/models`. The gateway already serves only the
routed paths.

## What gets transformed and restored

Both directions share one leaf walk (`proxy/schemas.py`) rather than a walker
per field, so a field a provider adds later is covered by default:

- **Restore walks every string leaf** of a batch response, whatever the schema.
  Placeholders exist only because Privyx minted them, so restoring anywhere is
  safe. Streaming restores deltas event by event (a token can split across
  them) and every other event — `content_block_start`, `citations_delta`,
  Responses `*.done`, `output_item.done`, `response.completed`, … — leaf by leaf,
  re-serializing it only when something changed.
- **Transform walks only the content subtrees** — the top-level keys `messages`,
  `system`, `input`, `instructions`, `prompt`, `prediction`. Every other
  top-level key is config (`model`, `tools`, `tool_choice`, `response_format`,
  Responses `text` / `reasoning`, `metadata`, …) and is forwarded as-is — except
  every `description` string inside `tools` (tool and parameter descriptions are
  prose an MCP server writes; names, `enum`, `pattern`, `default` stay). Inside
  a content subtree *every* string leaf is pseudonymized except the opaque keys,
  which are skipped with their whole subtree: `id` and anything ending in `_id`,
  `type`, `role`, `name` (tool/function names must survive), `signature`,
  `encrypted_*`, `data` (except an Anthropic plain-text document source),
  `file_data`, `url` and anything ending in `_url`, `media_type`,
  `cache_control`, `status`, and an `image_generation_call`'s base64 `result`.
  It fails closed: an unknown field is pseudonymized, never forwarded raw.
- **A conversation stays byte-stable across turns.** The request is walked in
  cache-prefix order (`tools`, `system` / `instructions`, then the rest), so a
  per-session counter cannot renumber the system prompt when a later message
  brings a new value. An echoed Anthropic `thinking` block gets back the exact
  text the upstream signed, remembered by `signature` from the batch or
  streamed response. Re-pseudonymizing its restored form is not always the
  inverse (a value the model wrote itself is new to the detector), and the
  provider rejects thinking whose text no longer matches its signature. The
  memory is per process: after a restart, or on another worker, the block falls
  back to being re-pseudonymized.
- **Tool arguments are documents.** An Anthropic `input` object and a parsed
  `arguments` string are user data with no wire structure, so no key inside
  them is opaque (a `name` argument is PII). A string `arguments` is parsed,
  walked, and re-serialized — never transformed as one blob, where a match could
  straddle a JSON escape — and walked as plain text if it does not parse.

Known gaps: a chat message's participant `name` is kept (it shares the key with
tool names); dict *keys* are never rewritten; `logprobs` token strings are not
restored, so a client that asks for them can see placeholder fragments.

## Errors

An error response from the upstream reaches the client as the upstream sent it.
When Privyx answers instead, the body is `{"error": {"type": …, "message": …}}`:

| Status | `type` | When |
|---|---|---|
| `400` | `privyx_invalid_request` | Gateway: the body is not a JSON object. Nothing is forwarded. |
| `502` | `privyx_upstream_unreachable` | No response from the upstream: the connection was refused or dropped. |
| `503` | `privyx_scan_failed` | Masking the request failed, so it was not forwarded. See [Detection](../guide/detection.md). |
| `503` | `privyx_upstream_busy` | All `proxy.max_connections` connections stayed busy for 10 s. Sent with `Retry-After: 10`. |
| `504` | `privyx_upstream_timeout` | The upstream took longer than `proxy.connect_timeout` to accept the connection or `proxy.timeout` to answer. |

The message ends with the error's class, such as `(ConnectError)`. Each of these
is also a `proxy.error` [audit event](../observability/audit-events.md), and the
upstream ones a single `WARNING` log line; the traceback is logged only at
`debug`. A stream that fails after its first bytes cannot change its status:
the connection is closed instead.

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

**Responses server-side history.** With `store: true` and `previous_response_id`,
the earlier turns live on the provider, *in pseudonym space*, and the client
sends only the new turn. Placeholders in that stored history only mean the same
thing on the next turn when the same session is reused (an `x-privyx-session`
header, or the `client` strategy — `conversation` fingerprints the first user
message, which a chained request no longer carries) or an anchor secret makes
them value-derived. A client that resends the full history each turn (as Codex
does by default) is not affected.

## Tool calls in a stream

OpenAI `tool_calls` (and the legacy `function_call`), Anthropic `tool_use`, and
Responses `function_call_arguments` / `mcp_call_arguments` /
`custom_tool_call_input` stream their arguments across many events. Forwarding
the partials would leak pseudonyms and fragment the JSON, so `StreamRouter`
accumulates them in pseudonym space and emits each — deanonymized, as one frame
— only once complete (dropping a truncated Chat call rather than forwarding an
unusable one). Terminal events (`[DONE]`, `message_stop`) are deferred and
emitted last, after every held-back text fragment and buffered tool call has
been flushed; Responses flushes every held-back delta before
`response.completed` / `.incomplete` / `.failed` / `error`.

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
