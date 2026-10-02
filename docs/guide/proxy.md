---
description: The two ways Privyx forwards requests, which paths it masks, what it does with every other path, and which API key reaches the provider.
---

# Proxy modes and routes

`privyx proxy` and `privyx run` put an HTTP proxy between your client and a
provider. This page covers how that proxy forwards requests: the two modes,
the paths it masks, what happens to every other path, and which key the
provider receives.

## Two modes

| | `transparent` (default) | `gateway` |
|---|---|---|
| Forwards a request to | the upstream's host, at the path the client asked for | one fixed URL, exactly as configured |
| Serves | every path | only the paths in `proxy.routes`, with `POST` |
| Masks | the paths in `proxy.routes` | every path it serves |
| Paths without a route | forwarded as sent, or refused | not served (`404`) |
| Key sent upstream | the client's, unless Privyx has one | only Privyx's own |

Select the mode with `--transparent` / `--gateway`, or `proxy.mode` in a
config file.

### Transparent

The drop-in mode. A client points its base URL at Privyx, and every request
goes to the same path on the provider:

```bash
privyx proxy --upstream https://api.openai.com
```

```text
client  → http://localhost:8000/v1/chat/completions
Privyx  → https://api.openai.com/v1/chat/completions      (masked)

client  → http://localhost:8000/v1/models
Privyx  → https://api.openai.com/v1/models                (as sent)
```

Only the scheme, host, and port of the upstream URL are used; a path in it is
ignored. "Transparent" means drop-in: this is an ordinary reverse proxy that
your client addresses on purpose, not a network-level interceptor.

### Gateway

Some providers serve their API under a base path:
`https://api.deepseek.com/anthropic/v1/messages`, an Azure deployment URL, a
router's prefix. Transparent mode mirrors the client's path, so it reaches
such an endpoint only when the client itself sends that prefix and a
[route](#adding-a-route) names it. In gateway mode Privyx posts every request
to the one URL you configure, whatever path the client used:

```yaml
# privyx.yaml
proxy:
  mode: gateway
  routes:
    /v1/messages: anthropic       # the path Privyx serves, and its wire schema
provider:
  type: anthropic                 # send the key as x-api-key
  base_url: https://api.deepseek.com/anthropic/v1/messages
```

```bash
export PRIVYX_ANTHROPIC_API_KEY=...
privyx proxy -c privyx.yaml
```

```text
client  → http://localhost:8000/v1/messages
Privyx  → https://api.deepseek.com/anthropic/v1/messages  (masked)
```

Three things differ from transparent mode:

- The client's own key is not relayed. Privyx sends the key it was given, or
  none.
- Every served path posts to the same URL, so list in `proxy.routes` only the
  paths that URL can answer. The token-counting and compaction paths are
  never served in this mode: forwarding one to a chat endpoint would turn a
  count into a billed completion.
- Anything else, such as `GET /v1/models`, gets a `404`.

Neither mode translates between wire schemas. An Anthropic client needs an
upstream that speaks the Anthropic format, in either mode.

## Routes

`proxy.routes` maps a request path to the wire schema of its body. A request
is masked only when its path is in this map. The default:

| Path | Schema | API |
|---|---|---|
| `/v1/chat/completions` | `openai` | OpenAI Chat Completions |
| `/v1/messages` | `anthropic` | Anthropic Messages |
| `/v1/messages/count_tokens` | `anthropic` | Anthropic token counting |
| `/v1/responses` | `responses` | OpenAI Responses |
| `/v1/responses/input_tokens` | `responses` | Responses token counting |
| `/v1/responses/compact` | `responses` | Responses compaction |
| `/chat/completions` | `openai` | |
| `/responses`, `/responses/input_tokens`, `/responses/compact` | `responses` | |

The path is matched exactly, without its query string, so
`/v1/messages?beta=true` matches too.

The last two rows are what an OpenAI client calls when its base URL lacks the
`/v1` it expects (`OPENAI_BASE_URL=http://localhost:8000`). They are masked
like the others and forwarded to the same path, which the provider may not
serve; set the base URL with `/v1`.

### Adding a route

A provider that serves a known format on another path needs a route for it.
Gemini's OpenAI-compatible endpoint, for example:

```yaml
proxy:
  routes:
    /v1beta/openai/chat/completions: openai
```

`routes` **replaces** the default map rather than extending it. List the
default paths as well if the same proxy also serves them. Add a route only
for a path whose body really has one of the three formats.

### Paths without a route

In transparent mode, a path that is not in `proxy.routes` is forwarded as the
client sent it. That keeps `GET /v1/models`, file uploads, and the rest of a
provider's API working, and it means that **nothing in those requests is
masked**. The common ones:

- `/v1/embeddings`
- the legacy `/v1/completions`
- Gemini's native `generateContent`
- reads of stored objects, such as `GET /v1/responses/{id}`

To close that gap, refuse them instead:

```yaml
proxy:
  passthrough_unknown: false
```

```console
$ curl -s http://localhost:8000/v1/embeddings -d '{"model": "text-embedding-3-small", "input": "ann@example.com"}'
{"error": {"message": "privyx: /v1/embeddings is not in proxy.routes and proxy.passthrough_unknown is false"}}
```

The answer is a `403`, for harmless paths such as `GET /v1/models` too.

### WebSocket

A WebSocket upgrade is answered `426 Upgrade Required` and never forwarded:
Privyx cannot relay one, so it could not mask its frames. Codex opens
`/v1/responses` as a WebSocket first and falls back to HTTP at once on that
status.

## What is masked in a request

On a routed path, Privyx masks the parts of the body that carry conversation
content and leaves the request's settings alone:

| Masked | Left as sent |
|---|---|
| Messages: text, tool results, tool-call arguments | `model`, sampling settings, `response_format` |
| The system prompt (`system`, `instructions`) | Tool names, parameter names, `enum` values |
| Responses `input` and `prompt` variables | Ids, roles, types, signatures |
| Tool and parameter descriptions | URLs, base64 data, images, files |
| What the client reports about its machine: Claude Code's `safeguards`, Codex's `client_metadata` and `x-codex-turn-metadata` header | Every other request header |

Inside the masked parts, a field Privyx does not know is masked, not passed
through: a field a provider adds later is covered from the start. The exact
rule is in
[Proxy architecture](../architecture/proxy.md#what-gets-transformed-and-restored).

## What is restored in a reply

- In a batch reply, every string: text, reasoning, tool-call arguments.
- In a stream, text and reasoning deltas as they arrive; a token split
  between two chunks is held back until it is whole. Tool-call arguments are
  collected and delivered restored in one piece.
- A token the model wrote without its outer delimiters (`PRIVYX_EMAIL_1`), or
  as its id alone when the id is long, is restored too. See
  [Token format](masking.md#token-format).

A token is restored only if the request's session issued it. A reply cannot
make Privyx reveal a value from another session.

## Keys and headers

**Which key reaches the provider.** The first of these that is set:

1. `provider.<type>_api_key` for the configured `provider.type`
   (`PRIVYX_OPENAI_API_KEY`, `PRIVYX_ANTHROPIC_API_KEY`);
2. `provider.api_key` (`PRIVYX_API_KEY`);
3. in transparent mode, the client's own `Authorization` or `x-api-key`; in
   gateway mode, none.

A key held by Privyx replaces the client's on every request. The transparent
proxy sends it as `x-api-key` on Anthropic paths and as a bearer token
elsewhere; the gateway sends it as `x-api-key` when `provider.type` is
`anthropic`. `proxy.forward_client_auth: false` drops the client's credential
headers before anything else, so only a configured key can go upstream.

**Headers Privyx adds or removes.**

- `x-privyx-session` on a request names the [session](sessions.md); Privyx
  removes it before forwarding and returns the session id it used in the
  same header of the response.
- `anthropic-version: 2023-06-01` is added on Anthropic paths when the client
  sent none.
- `provider.headers` in the config are added to every upstream request.
- Other request and response headers are relayed, so rate-limit and
  request-id headers from the provider reach the client.

## Endpoints Privyx answers itself

`GET /health` and `GET /metrics` are answered by Privyx in both modes and are
never forwarded. Neither needs a key. See
[Audit events and metrics](../observability/audit-events.md#consuming-the-trail).

## Timeouts and connection limits

| Setting | Default | Meaning |
|---|---|---|
| `proxy.timeout` | `300` | Seconds the upstream may take to send the next bytes of a response. A stream runs as long as data keeps coming. |
| `proxy.connect_timeout` | `10` | Seconds to open a connection. A connection that cannot be opened is retried twice. |
| `proxy.max_connections` | unset | Most connections open to the upstream at once. Unset means no limit. |

What the client sees when one of them is hit is listed in
[Errors](../reference/errors.md).

## Reloading the configuration

While you tune a config file, `--reload` restarts the proxy whenever the file
changes:

```bash
privyx proxy -c my.yaml --reload --upstream https://api.openai.com
```

It is a restart, not a hot swap: requests in flight get five seconds to
finish, and an in-memory vault starts empty again. A file that fails to parse
is reported, and the proxy waits for the next change instead of exiting. It is
meant for development.

## See also

- [Configuration reference](configuration.md#proxy): every `proxy` setting.
- [Deployment](deployment.md): TLS, binding, and running behind a reverse
  proxy.
- [Integrations](../integrations/index.md): the mode and routes each provider
  needs.
- [Proxy architecture](../architecture/proxy.md): how the proxy is built.
