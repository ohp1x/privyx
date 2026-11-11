# Google Gemini

Privyx masks Gemini's native API (`generateContent`, `streamGenerateContent`,
and `countTokens`) in both proxy modes. Gemini's
[OpenAI-compatible endpoint](#openai-compatible-endpoint) works too.

What a request has masked: the text of every turn, the system instruction,
function call arguments and function responses, and tool descriptions. What a
reply has restored: its text, streamed or not, thought summaries, and function
call arguments. Left as sent: inline data and file URIs (not inspected, like
any binary content), thought signatures, function names, and request settings
such as `generationConfig`.

## Transparent mode

The default routes cover the native paths of every API version
(`/v1beta/models/…`, `/v1/models/…`, `/v1alpha/models/…`) for any model,
embeddings (`embedContent`, `batchEmbedContents`), tuned models, and the
creation of cached contents included:

```bash
privyx proxy --upstream https://generativelanguage.googleapis.com
```

```python
from google import genai

client = genai.Client(
    api_key="<your Gemini API key>",  # relayed upstream as x-goog-api-key
    http_options={"base_url": "http://localhost:8000"},
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="My email is alice@example.com",
)
```

The client keeps its own key. To have Privyx send one instead, set
`provider.google_api_key` (`PRIVYX_GOOGLE_API_KEY`) with `provider.type: google`;
it then replaces the client's on Gemini paths.

A stream asked for with `alt=sse`, as the Google SDKs do, is restored event by
event. Without `alt=sse`, Gemini sends the stream as one JSON array; Privyx
reads it whole to restore it, so the client gets it at the end rather than as
it is generated.

## Gateway mode

Privyx holds the key, and the client calls Privyx's own Gemini paths:

```yaml
proxy:
  mode: gateway
  routes:
    /v1beta/models/*:generateContent: gemini
    /v1beta/models/*:streamGenerateContent: gemini
    /v1beta/models/*:countTokens: gemini
provider:
  type: google
```

```bash
export PRIVYX_GOOGLE_API_KEY=<your Gemini API key>
privyx proxy -c privyx.yaml
```

```text
client  → http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent
Privyx  → https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent  (masked)
```

Privyx posts to the path the client called, under `provider.base_url`. When
that is an origin (the default, `https://generativelanguage.googleapis.com`),
the client's API version is kept; when it has a path of its own, such as
Vertex AI's `…/v1/projects/PROJECT/locations/LOCATION/publishers/google`, that
path takes the place of the client's version. A URL that already ends in a
model and method is cut back to the part before `/models/`. The gateway serves Gemini paths only with
`provider.type: google`, and streams as SSE when the client asks for
`alt=sse`.

## Gemini CLI

```bash
export GEMINI_API_KEY=<your Gemini API key>
privyx run gemini
```

Gemini CLI goes through Privyx when it signs in with an API key; pick that in
its `/auth` menu if it signs in with Google. With Google sign-in or Vertex AI
it calls other endpoints, which the `gemini` profile also points at Privyx;
they are not masked, so Privyx refuses them with a `403` and Gemini CLI stops
with an error before any prompt leaves. For that launch, every path outside
`proxy.routes` is refused the same way. See
[`privyx run`](../guide/cli.md#privyx-run).

## OpenAI-compatible endpoint

Gemini also speaks the `openai` wire schema at
`https://generativelanguage.googleapis.com/v1beta/openai/`. Its chat path is
not in the default routes, so add it, **or it is forwarded verbatim**:

```yaml
proxy:
  routes:
    /v1beta/openai/chat/completions: openai
```

`proxy.routes` replaces the default map rather than merging into it; list the
default paths too if the same config also fronts the native API or other
providers (see [Adding a route](../guide/proxy.md#adding-a-route)).

```bash
privyx proxy -c privyx.yaml --upstream https://generativelanguage.googleapis.com
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1beta/openai/",
    api_key="<your Gemini API key>",  # relayed upstream as a bearer token
)
```

## Not covered

Forwarded verbatim in transparent mode (their query values are still masked,
except `key`). Keep PII out of them, or set `proxy.passthrough_unknown: false`
to answer them, and every other unrouted path, with a 403:

- `/v1beta/openai/embeddings`, unless you add a route
- file uploads and batch jobs
- Vertex AI paths (`/v1/projects/…/models/…:generateContent`): they use the
  same format, so a route makes them masked:
  `/v1/projects/*/locations/*/publishers/google/models/*:generateContent: gemini`
- the Live API, a WebSocket, which is refused with `426`

[`proxy.directive`](../guide/masking.md#placeholder-directive) goes last in
`systemInstruction`. It is not added to a `countTokens` call's own `contents`,
which take no instruction, nor to a request that uses a cached content, which
Gemini refuses one beside; a cached content created through Privyx has it
already.
