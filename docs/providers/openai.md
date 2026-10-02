# OpenAI

Point any OpenAI-compatible client at Privyx. In the default **transparent**
mode you only set the base URL — every path (chat, models, embeddings, …) is
forwarded, and the client's own `Authorization` header is relayed upstream
unless a key is configured on Privyx.

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

Privyx pseudonymizes the request, forwards to OpenAI, and deanonymizes the
streaming or batch response — including tool-call arguments. `/v1/chat/completions`
and the Responses API (below) are routed; `/v1/embeddings` and the legacy
`/v1/completions` are forwarded verbatim.

## Configuration

```yaml
provider:
  type: openai
  base_url: https://api.openai.com/v1/chat/completions
```

Do not write `api_key: ${VAR}`: Privyx does not expand environment variables in
config files, so the literal string is sent upstream as the key.

Privyx's own key comes, in both modes, from `PRIVYX_OPENAI_API_KEY` when
`provider.type` is `openai`, else from `PRIVYX_API_KEY` / `provider.api_key`.
When one is set it replaces the client's key on every request; when none is,
the transparent proxy relays the client's own `Authorization` header and the
gateway sends none.

## What is covered

Every string in `messages` is pseudonymized except opaque keys (ids, `role`,
`type`, `name`, `image_url`, base64 `data` — the full rule is in
[proxy.md](../architecture/proxy.md#what-gets-transformed-and-restored)):
`content` (a string or parts, including `refusal` parts), `refusal`,
`reasoning_content` and the non-standard `reasoning` (OpenRouter / vLLM /
Ollama), `tool_calls[].function.arguments` and the legacy
`function_call.arguments` (walked as parsed JSON), and `prediction.content`.
`tools` is config apart from its `description` strings (function and parameter
descriptions); `response_format`, `metadata`, `user`, and the rest of the top
level are left alone. A batch response is restored at every string leaf.

## Streaming

OpenAI SSE deltas live at `choices[0].delta` — `content`, `reasoning_content`,
`reasoning`, and `refusal` are each deanonymized on their own buffer, and
`tool_calls` / legacy `function_call` arguments are buffered and restored as a
complete call. Every other chunk (`role`, `finish_reason`, usage) is restored
leaf by leaf.

## Responses API

`/v1/responses` — the wire API Codex CLI speaks, so `privyx run codex` is
covered — is routed with the `responses` schema, as are
`/v1/responses/input_tokens` and `/v1/responses/compact` (same body).

- **Request.** `instructions`, `prompt.variables`, and `input` — a bare string, or
  an item list — are pseudonymized: message `input_text` / `output_text`,
  `reasoning` summaries and content (`encrypted_content` is opaque and kept),
  `function_call.arguments` (parsed JSON), `function_call_output.output`,
  `custom_tool_call` input / output, `local_shell_call.action.command`, shell
  output `stdout` / `stderr`, `apply_patch_call.operation.diff`, `mcp_call`
  `arguments` / `output`, and any string field added later. Top-level `text`,
  `reasoning`, and `tool_choice` are config and left alone; so is `tools`, apart
  from its `description` strings.
- **Stream.** Every event with a string `delta` (`output_text`, `refusal`,
  `reasoning_summary_text`, `reasoning_text`, `audio.transcript`, …) is restored
  per stream, keyed by kind and `item_id` / `output_index` / `content_index` /
  `summary_index`. `function_call_arguments`, `mcp_call_arguments`, and
  `custom_tool_call_input` are buffered and emitted as one restored delta just
  before their `.done`. The full text is repeated in `*.done`,
  `content_part.done`, `output_item.done`, and `response.completed` — which
  clients such as Codex build history from — so those are restored too, with
  their `event:` lines kept. A flushed tail reuses its template's
  `sequence_number`.
- **Server-side history.** `previous_response_id` with `store: true` keeps
  earlier turns on the provider in pseudonym space; see
  [proxy.md](../architecture/proxy.md#sessions) for when those placeholders stay
  consistent.
