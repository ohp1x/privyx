# Anthropic

Point the Anthropic SDK at Privyx. In the default **transparent** mode the
`/v1/messages` path is routed and transformed, and the client's `x-api-key` /
`anthropic-version` headers are forwarded upstream.

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

## Streaming, reasoning & tools

Anthropic's `content_block_delta` events carry `delta.text` for text,
`delta.thinking` for reasoning, and `delta.input_json_delta` for tool inputs.
`StreamRouter` deanonymizes text and thinking on separate buffers, and
accumulates a `tool_use` block's partial JSON until `content_block_stop`, then
emits it restored in one frame. `signature_delta` and other control events pass
through untouched, and `message_stop` is emitted last so nothing arrives after
it. Note `system` may be a string or a list of text blocks — both are
pseudonymized.
