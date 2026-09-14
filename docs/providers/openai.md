# OpenAI

Point any OpenAI-compatible client at Privyx. In the default **transparent**
mode you only set the base URL — every path (chat, models, embeddings, …) is
forwarded, and the client's own `Authorization` header is relayed upstream
unless a key is configured on Privyx.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",  # Privyx (transparent) proxy
    api_key="your-key",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "My email is alice@example.com"}],
)
```

Start it with:

```bash
privyx proxy --transparent --upstream https://api.openai.com
```

Privyx pseudonymizes the request, forwards to OpenAI, and deanonymizes the
streaming or batch response — including tool-call arguments.

## Configuration

```yaml
provider:
  type: openai
  base_url: https://api.openai.com/v1/chat/completions
  api_key: ${PRIVYX_OPENAI_API_KEY}
```

## Streaming

OpenAI SSE deltas live at `choices[0].delta.content` — handled by
`OpenAIStreamAdapter`. `reasoning_content` (reasoning models) is deanonymized on
its own buffer, and `tool_calls` arguments are buffered and restored as a
complete call. Envelope-only deltas (`role`, `finish_reason`) pass through
untouched.
