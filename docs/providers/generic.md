---
description: Put Privyx in front of any endpoint that speaks the OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages format, and add a provider of your own.
---

# Any other endpoint

Privyx does not need to know a provider by name. It needs to know two things:
where to send requests, and which of the three wire formats a path speaks
(OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages). A request
in another format is forwarded, not masked.

## A compatible API on standard paths

If the service uses the standard paths (`/v1/chat/completions`,
`/v1/messages`, `/v1/responses`), an upstream is all it takes:

```bash
privyx proxy --upstream https://llm.example.com
```

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
```

## A compatible API on other paths

Two options, both described in [Proxy modes and routes](../guide/proxy.md):

- **Add a route**, when the client can send the provider's path itself:

    ```yaml
    proxy:
      routes:
        /api/v1/chat/completions: openai
    ```

- **Use gateway mode**, when the client cannot, and every request should go
  to one fixed URL:

    ```yaml
    proxy:
      mode: gateway
      routes:
        /v1/chat/completions: openai
    provider:
      type: generic
      base_url: https://llm.example.com/some/base/path/chat/completions
    ```

## The `generic` provider type

`provider.type` decides the default upstream and how a key held by Privyx is
sent. `generic` is the default type:

```yaml
provider:
  type: generic
  base_url: http://localhost:20128   # the default when nothing else is set
  api_key: ""                        # or PRIVYX_API_KEY; sent as a bearer token
  headers: {}                        # extra headers for every upstream request
```

Use `openai` or `anthropic` instead to get that provider's default URL, and
for `anthropic` the `x-api-key` header.

## A provider of your own

A provider is the transport the gateway uses to reach an upstream. To add
one, subclass `BaseProvider` (or reuse `GenericProvider`) in a
[plugin](../development/plugins.md) and select it with `provider.type`. If its
stream is not SSE, it also needs a stream adapter in `streaming/adapters/`.
