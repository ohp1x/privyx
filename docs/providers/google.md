# Google Gemini

There is no native Gemini provider yet. `provider.type: google` is not
registered (the gateway refuses to start with `unknown provider: google`), and Gemini's native
`generateContent` / `streamGenerateContent` paths are not routed — a transparent
proxy forwards them **verbatim**, so any PII in them reaches Google in the
clear. A native route is planned for v0.2.0.

Until then, use Gemini's [OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai)
(`https://generativelanguage.googleapis.com/v1beta/openai/`). It speaks the
`openai` wire schema, so chat messages, streaming deltas, and tool-call
arguments are pseudonymized and restored exactly as described in
[openai.md](openai.md).

## Gateway mode

The simplest setup: Privyx serves `/v1/chat/completions` and posts it to the
Gemini endpoint.

```yaml
proxy:
  mode: gateway
  routes:
    /v1/chat/completions: openai
provider:
  type: generic
  base_url: https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
```

```bash
export PRIVYX_API_KEY=<your Gemini API key>
privyx proxy -c privyx.yaml
```

The gateway does not relay the client's key; Privyx sends its own, from
`PRIVYX_API_KEY` (or `provider.api_key`). `provider.type: generic` matters here:
with `openai`, an exported `PRIVYX_OPENAI_API_KEY` would take precedence and
your OpenAI key would be sent to Google. The wire schema comes from `routes`,
not from the provider type. Narrowing `routes` to the one chat path keeps
Anthropic and Responses requests from being posted to Gemini.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[{"role": "user", "content": "My email is alice@example.com"}],
)
```

## Transparent mode

Use this when the client should keep its own key, or needs other Gemini paths
(such as `/v1beta/openai/models`) to reach the upstream. The Gemini chat path is
not in the default routes, so add it — **without the route it is forwarded
verbatim**:

```yaml
proxy:
  routes:
    /v1beta/openai/chat/completions: openai
```

`proxy.routes` replaces the default map rather than merging into it; list the
default paths too if the same config also fronts OpenAI or Anthropic (see
[Adding a route](../guide/proxy.md#adding-a-route)).

```bash
privyx proxy --transparent -c privyx.yaml --upstream https://generativelanguage.googleapis.com
```

```python
client = OpenAI(
    base_url="http://localhost:8000/v1beta/openai/",
    api_key="<your Gemini API key>",  # relayed upstream as a bearer token
)
```

## Not covered

Forwarded verbatim in transparent mode — keep PII out of them, or set
`proxy.passthrough_unknown: false` to answer them (and every other unrouted
path, including `/v1beta/openai/models`) with a 403 instead:

- `/v1beta/openai/embeddings`
- native `/v1beta/models/{model}:generateContent` and `:streamGenerateContent`
