# OpenAI

Point any OpenAI-compatible client at the Privyx gateway:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",  # Privyx gateway
    api_key="your-key",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "My email is alice@example.com"}],
)
```

Privyx pseudonymizes the request, forwards to OpenAI, and deanonymizes the
streaming or batch response.

## Configuration

```yaml
provider:
  type: openai
  base_url: https://api.openai.com/v1/chat/completions
  api_key: ${PRIVYX_OPENAI_API_KEY}
```

## Streaming

OpenAI SSE deltas live at `choices[0].delta.content` — handled by
`OpenAIStreamAdapter`. Non-content deltas (`role`, `finish_reason`,
`reasoning_content` on reasoning models) pass through untouched.
