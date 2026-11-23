---
description: "How the Privyx proxy is built: the two serving modes, which parts of a request are masked and restored, sessions, and streamed tool calls."
---

# Proxy architecture

This page describes how the proxy is built. To pick a mode, add a route, set
up HTTPS, or look up an error, see [Proxy modes and routes](../guide/proxy.md),
[Deployment](../guide/deployment.md), and [Errors](../reference/errors.md).

## Two serving modes

`privyx proxy` serves one of two apps, selected by `proxy.mode` (or
`--transparent` / `--gateway`):

- **`transparent`** (default): a drop-in reverse proxy. A client points its
  base URL at Privyx, and every path is forwarded to the upstream's origin.
  The paths in `proxy.routes` are masked on the way out and restored on the
  way back, batch or streamed. Every other path (models, files, and so on) is
  forwarded as sent and streamed in both directions, so a large upload or
  download is never held in memory. "Transparent" means drop-in: this is an
  HTTP reverse proxy, not a network-level interceptor.
- **`gateway`**: Privyx's own endpoints, all forwarded to the one upstream
  endpoint that `provider.base_url` names (`resolve_base_url`), used as
  written; the incoming path is not passed on. The same `proxy.routes` map
  says which paths are served and which wire schema each speaks. Every served
  path posts to that endpoint, except embeddings and legacy completions,
  which post beside it (`…/v1/embeddings`). The token-counting and compaction
  routes are not served, since forwarding one to a chat endpoint would turn a
  count into a billed completion. A default gateway also forwards
  `/v1/responses` to that endpoint, so narrow `proxy.routes` to the paths the
  upstream actually speaks.

Transparent mode keeps only the upstream's origin: `resolve_origin` cuts
`base_url` back to `scheme://host[:port]` and appends the client's path. With
`provider.path_mapping.mode: prefix` it keeps the upstream's path and appends the
client's path after `strip_prefix`. When to use which mode is covered in
[Proxy modes and routes](../guide/proxy.md#two-modes).

Neither mode translates between wire schemas. Privyx only knows where the text
sits in each one, so an Anthropic client still needs an upstream that speaks
the Anthropic format.

`--reload` watches the config files in use, not the source code. When one
changes, uvicorn shuts down gracefully and the engine, vault, plugins, and
provider are built again from the new settings. Requests in flight get 5 s to
finish, as on SIGTERM, and the listener is briefly gone. A config that fails
to parse or to build is reported, and the watcher waits for the next change.

Both modes run through one pipeline (`proxy/_pipeline.py`), with one
implementation of request masking and batch restore (`proxy/_mask.py`) and of
streaming restore (`streaming/_router.py`). The transparent proxy adds path
routing, per-request schema selection, and the forwarding of headers and
credentials. `proxy/_asgi.py`, the one module that knows FastAPI, turns each
HTTP request into an exchange for the pipeline and holds no privacy logic.

## Routes

The default routes, how to add one, and what happens to a path without a
route are covered in [Proxy modes and routes](../guide/proxy.md#routes).
`proxy.routes` is looked up by path (`proxy/_routing.py::matched_route`), with
the query string already split off. Exact routes win over wildcard routes;
`*` matches characters other than `/`, and the first matching wildcard wins.
An accidental repeated `/v1/v1/` prefix also uses the intended `/v1/` route
so a request that the upstream will reject is still masked.
A Gemini path without its API version (`/models/m:generateContent`), which a
client calls when its base URL ends in the version, uses the Gemini route it
stands for, so it is masked whatever the upstream answers.

## What gets transformed and restored

Both directions share one leaf walk (`core/_walk.py`) rather than a walker
per field, so a field a provider adds later is covered by default:

- **Restore walks every string leaf** of a batch response, whatever the schema.
  Placeholders exist only because Privyx minted them, so restoring anywhere is
  safe. A token the model wrote without its outer delimiters (`PRIVYX_EMAIL_1`)
  or one of them, in another case, or as its id alone when the id is long enough,
  is restored as well, in a batch and in a stream alike, if the session issued
  it; see [Token format](../guide/masking.md#token-format). Streaming restores deltas event by event (a token can split across
  them), and every other event (`content_block_start`, `citations_delta`,
  Responses `*.done`, `output_item.done`, `response.completed`, and so on)
  leaf by leaf, re-serializing it only when something changed.
- **Transform walks only the content subtrees:** the top-level keys `messages`,
  `system`, `input`, `instructions`, `prompt`, `suffix`, and `prediction`. Every other
  top-level key is config (`model`, `tools`, `tool_choice`, `response_format`,
  Responses `text` / `reasoning`, `metadata`, and so on) and is forwarded as
  is, except every `description` string inside `tools` (tool and parameter descriptions are
  prose an MCP server writes; names, `enum`, `pattern`, `default` stay) and what
  the client reports about its machine (below). Inside
  a content subtree *every* string leaf is pseudonymized except the opaque keys,
  which are skipped with their whole subtree: `id` and anything ending in `_id`,
  `type`, `role`, `name` (tool/function names must survive), `signature`,
  `encrypted_*`, `data` (except an Anthropic plain-text document source),
  `file_data`, `url` and anything ending in `_url`, `media_type`,
  `cache_control`, `status`, and an `image_generation_call`'s base64 `result`.
  It fails closed: an unknown field is pseudonymized, never forwarded raw.
- **Native schemas select their own content.** Gemini walks `contents`,
  `systemInstruction`, embedding content and titles, and wrapped token-count
  and batch embedding requests. Ollama also masks its `prompt`, `suffix`,
  and messages. Binary image data, media types, and native signatures stay
  opaque. See [Gemini](../providers/google.md) and
  [Ollama](../providers/local.md#ollamas-own-api).
- **A conversation stays byte-stable across turns.** The request is walked in
  cache-prefix order (`tools`, `system` / `instructions`, then the rest), so a
  per-session counter cannot renumber the system prompt when a later message
  brings a new value. A tool sent with `defer_loading` (one a client loaded
  with tool search) is not part of that prefix: it is walked right after the
  message whose `tool_reference` loads it, where the provider renders it, or
  last while nothing loads it. An echoed Anthropic `thinking` block gets back the exact
  text the upstream signed, remembered by `signature` from the batch or
  streamed response. Re-pseudonymizing its restored form is not always the
  inverse (a value the model wrote itself is new to the detector), and the
  provider rejects thinking whose text no longer matches its signature. The
  memory is per process: after a restart, or on another worker, the block falls
  back to being re-pseudonymized. With `proxy.dlp_check`, the masked request is
  checked before remembered thinking is reinstated: only the upstream's exact
  text bypasses the check, while all client fields remain checked.
- **Tool arguments are documents.** An Anthropic `input` object and a parsed
  `arguments` string are user data with no wire structure, so no key inside
  them is opaque (a `name` argument is PII). A string `arguments` is parsed,
  walked, and re-serialized, never transformed as one blob, where a match could
  straddle a JSON escape. One that does not parse is walked as plain text.
  Each string of up to 256 characters in one is also checked together with its
  key, so a value that only its key marks, as in `{"password": "hunter2"}`, or
  whose context word is the key, is masked. Numbers are checked alone, so
  `{"token_limit": 4000}` stays a number and a card number is still masked; a
  masked number becomes its token, a string, and is restored as one.
- **What a client reports about its machine is masked whole, and last.** Claude
  Code's `safeguards` (working directory, home, user name, git branch and
  remotes) and Codex's `client_metadata` (workspace paths, git remote URLs) are
  walked with no opaque key: there a file list sits under `status` and a remote
  under `url`. They are walked after everything else, because a client may send
  `safeguards` with only some requests of a conversation: numbered earlier, its
  values would shift every token after them on those turns alone. Codex repeats its metadata
  in the `x-codex-turn-metadata` request header, which the transparent proxy
  masks in the same session, so a path gets one token in the body and in the
  header. That holds on any path and method, a request without a body
  included. A value that is itself JSON, as that
  metadata is, has its `\uXXXX` escapes decoded and is then pseudonymized as
  text, since the paths in it are keys; the header goes out as ASCII again. No
  other request header is masked.

Known gaps: a chat message's participant `name` is kept (it shares the key with
tool names); dict *keys* are never rewritten; `logprobs` token strings are not
restored, so a client that asks for them can see placeholder fragments.

## Errors

The responses Privyx generates itself, with their status and `error.type`,
are listed in [Errors](../reference/errors.md). Both modes build them in one
place (`proxy/_upstream.py::upstream_failure` and the `*_BODY` constants in
`proxy/_pipeline.py`), so they answer alike.

## Layers

The proxy is deliberately thin. It:

1. Decodes incoming HTTP requests (JSON body).
2. Hands the text to the privacy engine, which masks it.
3. Forwards to the upstream provider.
4. Handles the response, batch or streamed.
5. Restores it before returning it to the client.

## Sessions

A session owns the token-to-value mapping used to restore a response. An
explicit `x-privyx-session` header always picks the session. Most clients
(Claude Code, Codex, aider, the OpenAI CLI) never send one, so
`session.strategy` decides (`proxy/_session.py::resolve_session_id`, applied by
both modes before `get_or_create_session`):

- **`ephemeral`** (default): a fresh session per request, kept in the proxy's
  memory and never in the vault. Masking and restoring happen within one
  exchange, and the client only ever sees restored text, so a per-request
  session is correct and, unlike one shared session, cannot mix two callers'
  mappings. The cost: a multi-turn conversation is detected again on every
  turn and shows up as many sessions, and without an anchor its tokens carry
  no meaning from one turn to the next.
- **`client`**: a stable session derived from the client's credential. Simple,
  but every conversation under that credential shares one growing mapping.
- **`conversation`**: derived from the credential and the first user message,
  so each conversation reuses its own session while different conversations,
  even under the same key, stay apart. This is what `privyx run` uses.

Because the id is derived from the request, continuity is automatic:
`get_or_create_session` reuses the vault session whenever the id exists, so
tokens stay stable across turns, and only the first turn logs
`session.created`. A derived id is keyed on the client's credential when the
request carries one, so callers with different credentials get different
sessions. Callers that share a credential can share a session, and so can
callers without one under `conversation`, whose id then comes from the first
user message alone. Whoever shares a session can have its tokens restored. The
`session.created` audit event records a `source` (`header`, `client`,
`conversation`, or `ephemeral`), so the trail shows how each id was chosen.

**The trade-off of `conversation`.** The fingerprint is the first user
message, which stays the same across a conversation's turns. Two
conversations under the same key that open with an identical first message
therefore share a session, and editing or clearing the first message starts a
new one. That is usually what you want, but it is a heuristic. When exact
isolation matters, give each caller its own credential, or have a trusted
backend set `x-privyx-session` to an opaque id. The header is honored as sent,
so an ingress in front of other clients must remove it; see
[Sessions](../guide/sessions.md#which-session-a-request-uses).

For tokens that stay the same without a shared session, across restarts and
across conversations, set an anchor secret (`PRIVYX_ANCHOR_SECRET`): the token
is then derived from the value instead of counted per session. `privyx run`
sets one up by itself.

**Responses server-side history.** With `store: true` and
`previous_response_id`, the earlier turns are stored at the provider, with
tokens in them, and the client sends only the new turn. Those tokens mean the
same values on the next turn only when the same session is used again (an
`x-privyx-session` header, or the `client` strategy; `conversation` needs the
first user message, which a chained request no longer carries), or when an
anchor secret derives them from the values. A client that resends the full
history each turn, as Codex does by default, is not affected.

## Tool calls in a stream

OpenAI `tool_calls` (and the legacy `function_call`), Anthropic `tool_use`, and
Responses `function_call_arguments`, `mcp_call_arguments`, and
`custom_tool_call_input` stream their arguments across many events. Passing
the pieces on would deliver tokens and broken JSON, so `StreamRouter` collects
them and emits each call restored, as one frame, once it is complete. A
truncated Chat Completions call is dropped rather than forwarded unusable.
Terminal events (`[DONE]`, `message_stop`) are held back and emitted last,
after every held-back text fragment and buffered tool call; Responses emits
every held-back delta before `response.completed`, `.incomplete`, `.failed`,
or `error`.

## Streaming path

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

## Why the SSE envelope is not the text stream

SSE events carry metadata (`event:`, `id:`, `retry:`), multi-line `data:`,
and JSON payloads that embed text at different paths (e.g.
`choices[0].delta.content` for OpenAI, `delta.text` for Anthropic).

If the proxy treated raw SSE bytes as text, it would corrupt envelopes and
transform metadata it should leave alone. The adapter layer separates
envelope handling from text transformation.
