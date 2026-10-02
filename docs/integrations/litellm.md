---
description: Route LiteLLM SDK calls through Privyx with api_base, for OpenAI and Anthropic models.
---

# LiteLLM

The LiteLLM SDK takes an `api_base` per call. Point it at Privyx:

## OpenAI models

```bash
privyx proxy --upstream https://api.openai.com
```

```python
import litellm

reply = litellm.completion(
    model="openai/gpt-4o-mini",
    api_base="http://localhost:8000/v1",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

## Anthropic models

```bash
privyx proxy --upstream https://api.anthropic.com
```

```python
import litellm

reply = litellm.completion(
    model="anthropic/claude-opus-5-5",
    api_base="http://localhost:8000",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

LiteLLM speaks the Anthropic format to an `anthropic/` model, so this request
arrives at Privyx on `/v1/messages`.

## Without changing code

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
export ANTHROPIC_BASE_URL=http://localhost:8000
```

## Good to know

- **One Privyx per upstream.** LiteLLM can talk to many providers; Privyx
  forwards to one. Run a Privyx instance per provider you want masked, each
  on its own port, and give each model its `api_base`.
- **Other providers.** For a provider LiteLLM reaches in the OpenAI format,
  use the `openai/` prefix with the model name and a Privyx instance whose
  upstream is that provider.
