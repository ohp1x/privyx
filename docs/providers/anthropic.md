# Anthropic

Point the Anthropic SDK at Privyx. In the default **transparent** mode
`/v1/messages` and `/v1/messages/count_tokens` (the same body) are routed and
transformed, and the client's `x-api-key` / `anthropic-version` headers are
forwarded upstream.

```python
import anthropic

client = anthropic.Anthropic(base_url="http://localhost:8000")  # the key still comes from ANTHROPIC_API_KEY

reply = client.messages.create(
    model="claude-opus-5-5",
    max_tokens=16000,
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(next(block.text for block in reply.content if block.type == "text"))
```

Start it with:

```bash
privyx proxy --upstream https://api.anthropic.com
```

The base URL has no `/v1`: the SDK adds `/v1/messages` itself. Streaming and
tool calls are shown in
[An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md), and Claude
Code in [Coding agents](../tutorials/coding-agents.md).

## Configuration

```yaml
provider:
  type: anthropic
  base_url: https://api.anthropic.com/v1/messages
  headers:
    anthropic-version: "2023-06-01"
```

Do not write `api_key: ${VAR}`: Privyx does not expand environment variables in
config files, so the literal string is sent upstream as the key.

Privyx's own key comes, in both modes, from `PRIVYX_ANTHROPIC_API_KEY` when
`provider.type` is `anthropic`, else from `PRIVYX_API_KEY` / `provider.api_key`.
When one is set it replaces the client's key on every request; when none is,
the transparent proxy relays the client's own `x-api-key` header and the
gateway sends none.

## What is covered

`system` (a string or text blocks) and every string in `messages` are
pseudonymized, except opaque keys — ids, `type`, `name`, `signature`, base64
`data`, URLs, `media_type`, `cache_control` (the full rule is in
[proxy.md](../architecture/proxy.md#what-gets-transformed-and-restored)). That
reaches, among others, `text` / `thinking`, `tool_use` and `server_tool_use`
`input` (every leaf, whatever its key), `tool_result` content, `document` blocks
(a plain-text `source.data`, `source.content`, `title`, `context`),
`search_result` (`title`, `content[].text`), `citations[].cited_text`, and
code-execution results (`stdout` / `stderr`). `tools` is config apart from its
`description` strings (tool and `input_schema` parameter descriptions);
`tool_choice`, `metadata`, and the rest of the top level are left alone. A batch
response is restored at every string leaf.

## Streaming, reasoning & tools

Anthropic's `content_block_delta` events carry `delta.text` for text,
`delta.thinking` for reasoning, and `delta.input_json_delta` for tool inputs.
`StreamRouter` deanonymizes text and thinking on separate buffers, and
accumulates a `tool_use` / `server_tool_use` block's partial JSON until
`content_block_stop`, then emits it restored in one frame. Every other event —
`citations_delta` (a complete `cited_text`), a `content_block_start` carrying a
whole server-tool result, `message_start` — is restored leaf by leaf;
`signature` values are never touched, and `message_stop` is emitted last so
nothing arrives after it. The thinking text a signature covers is remembered as
the upstream sent it, so when the client echoes the block back it goes upstream
byte-identical, not re-pseudonymized
([proxy.md](../architecture/proxy.md#what-gets-transformed-and-restored)).
