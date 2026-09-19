# Anthropic

Point the Anthropic SDK at Privyx. In the default **transparent** mode
`/v1/messages` and `/v1/messages/count_tokens` (the same body) are routed and
transformed, and the client's `x-api-key` / `anthropic-version` headers are
forwarded upstream.

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000",  # Privyx (transparent) proxy
    api_key="your-key",
)

message = client.messages.create(
    model="claude-3-5-sonnet-latest",
    max_tokens=1024,
    messages=[{"role": "user", "content": "My phone is +1 (555) 123-4567"}],
)
```

Start it with:

```bash
privyx proxy --transparent --upstream https://api.anthropic.com
```

## Configuration

```yaml
provider:
  type: anthropic
  base_url: https://api.anthropic.com/v1/messages
  api_key: ${PRIVYX_ANTHROPIC_API_KEY}
  headers:
    anthropic-version: "2023-06-01"
```

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
