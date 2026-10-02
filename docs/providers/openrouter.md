---
description: Use OpenRouter through Privyx. OpenRouter serves its API under /api/v1, so the proxy needs one route.
---

# OpenRouter

OpenRouter speaks the OpenAI Chat Completions format at
`https://openrouter.ai/api/v1`. That path is not among Privyx's default
routes, so add it. Without the route, requests are forwarded **unmasked**.

## Configure

```yaml
# openrouter.yaml
proxy:
  routes:
    /api/v1/chat/completions: openai
```

```bash
privyx proxy -c openrouter.yaml --upstream https://openrouter.ai
```

## Connect a client

The client keeps OpenRouter's `/api/v1` in its base URL and its OpenRouter
key, which Privyx relays:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

reply = client.chat.completions.create(
    model="openai/gpt-4o-mini",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

Optional headers such as `HTTP-Referer` pass through unchanged.

## Good to know

- `proxy.routes` replaces the default map. If the same proxy should also
  serve the standard paths, list them too; see
  [Routes](../guide/proxy.md#routes).
- Other OpenRouter paths, such as `/api/v1/models`, are forwarded as sent.
  Set [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route)
  to refuse anything that is not masked.
- To check the route, run `privyx audit tail` after a request:
  `session.transform` appears only for masked paths, and `proxy.request`
  shows `schema=openai`.
