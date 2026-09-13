# Anthropic

Point the Anthropic SDK at the Privyx gateway:

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000",
    api_key="your-key",
)

message = client.messages.create(
    model="claude-3-5-sonnet-latest",
    max_tokens=1024,
    messages=[{"role": "user", "content": "My phone is +1 (555) 123-4567"}],
)
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

## Streaming & Reasoning

Anthropic's `content_block_delta` events carry `delta.text` for text and
`delta.thinking` for reasoning. `AnthropicStreamAdapter` transforms only
`text_delta` events — thinking deltas pass through untouched, avoiding the
corruption that naive proxies suffer with `reasoning_content`.
