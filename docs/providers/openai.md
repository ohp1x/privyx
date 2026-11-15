---
description: "Use the OpenAI API through Privyx: setup, keys, and what is masked and restored in Chat Completions, embeddings, and the Responses API."
---

# OpenAI

Point any OpenAI-compatible client at Privyx. In the default transparent mode
only the base URL changes: every path is forwarded, and the client's own
`Authorization` header goes upstream unless Privyx holds a key.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1")  # the key still comes from OPENAI_API_KEY

reply = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

Start it with:

```bash
privyx proxy --upstream https://api.openai.com
```

Streaming, tool calls, and the Responses API are shown in
[An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md), and Codex in
[Coding agents](../tutorials/coding-agents.md).

Privyx masks the request, forwards it to OpenAI, and restores the reply,
streamed or not, tool-call arguments included. It masks
`/v1/chat/completions`, the legacy `/v1/completions`, `/v1/embeddings`, and
the [Responses API](#responses-api). An embeddings reply holds only vectors,
so it is returned as it is; see [Embeddings](../guide/proxy.md#embeddings).

## Configuration

Transparent mode needs no config file. `provider.type: openai` makes OpenAI
the upstream when `--upstream` is not given: transparent mode uses the origin
of `base_url`, and gateway mode posts to it as written. The value below is the
default.

```yaml
provider:
  type: openai
  base_url: https://api.openai.com/v1/chat/completions
```

Credential settings can read environment variables, such as
`api_key: ${OPENAI_API_KEY}`. A missing variable stops startup; see
[Configuration](../guide/configuration.md#where-settings-come-from).

Privyx's own key comes, in both modes, from `PRIVYX_OPENAI_API_KEY` when
`provider.type` is `openai`, else from `PRIVYX_API_KEY` / `provider.api_key`.
When one is set it replaces the client's key on every request; when none is,
the transparent proxy relays the client's own `Authorization` header and the
gateway sends none.

## What is covered

Every string in `messages` is masked, except under keys that hold no prose,
such as ids, `role`, `type`, `name`, `image_url`, and base64 `data`; the full
rule is in
[Proxy architecture](../architecture/proxy.md#what-gets-transformed-and-restored).
That covers `content` (a string or parts, `refusal` parts included),
`refusal`, `reasoning_content` and the non-standard `reasoning` (OpenRouter,
vLLM, Ollama), `tool_calls[].function.arguments` and the legacy
`function_call.arguments` (parsed as JSON and masked value by value), and
`prediction.content`. In `tools`, only the function and parameter
descriptions are masked; `response_format`, `metadata`, `user`, and the rest
of the top level are left alone. In a batch reply, every string is restored.

## Streaming

In a stream, `content`, `reasoning_content`, `reasoning`, and `refusal` in
`choices[0].delta` are each restored separately as they arrive. The arguments
of `tool_calls` and the legacy `function_call` are collected and delivered
restored as one complete call. Every other chunk (`role`, `finish_reason`,
usage) has each of its strings restored.

## Responses API

`/v1/responses` is masked with the `responses` schema, and so are
`/v1/responses/input_tokens` and `/v1/responses/compact`, which take the same
body. It is the API Codex speaks, so `privyx run codex` is covered.

- **Request.** `instructions`, `prompt.variables`, and `input`, a string or a
  list of items, are masked: message `input_text` / `output_text`,
  `reasoning` summaries and content (`encrypted_content` is opaque and kept),
  `function_call.arguments` (parsed JSON), `function_call_output.output`,
  `custom_tool_call` input / output, `local_shell_call.action.command`, shell
  output `stdout` / `stderr`, `apply_patch_call.operation.diff`, `mcp_call`
  `arguments` / `output`, and any string field added later. Top-level `text`,
  `reasoning`, and `tool_choice` are config and left alone; so is `tools`, apart
  from its `description` strings.
- **Stream.** Every event with a string `delta` (`output_text`, `refusal`,
  `reasoning_summary_text`, `reasoning_text`, `audio.transcript`, and others)
  is restored separately for each kind and `item_id`, `output_index`,
  `content_index`, and `summary_index`. `function_call_arguments`, `mcp_call_arguments`, and
  `custom_tool_call_input` are buffered and emitted as one restored delta just
  before their `.done`. The full text is repeated in `*.done`,
  `content_part.done`, `output_item.done`, and `response.completed`, which
  clients such as Codex build their history from, so those are restored too,
  with their `event:` lines kept. A flushed tail reuses its template's
  `sequence_number`.
- **Server-side history.** `previous_response_id` with `store: true` keeps
  earlier turns on the provider, with tokens in them; see
  [Proxy architecture](../architecture/proxy.md#sessions) for when those
  tokens keep their meaning.
