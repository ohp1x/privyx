---
description: Use DeepSeek through Privyx, with its OpenAI-compatible API, its Anthropic-compatible API, or Claude Code.
---

# DeepSeek

DeepSeek offers two formats: the OpenAI format at `https://api.deepseek.com`,
and the Anthropic format under `https://api.deepseek.com/anthropic`.

## OpenAI format

DeepSeek serves chat completions at `/chat/completions`, without `/v1`. That
path is among Privyx's default routes:

```bash
privyx proxy --upstream https://api.deepseek.com
```

```python
import os

from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000", api_key=os.environ["DEEPSEEK_API_KEY"])

reply = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

## Anthropic format

The Anthropic-compatible API sits behind a base path. A client that sends
that path itself needs a route for it:

```yaml
# deepseek.yaml
proxy:
  routes:
    /anthropic/v1/messages: anthropic
```

```bash
privyx proxy -c deepseek.yaml --upstream https://api.deepseek.com
```

```python
import os

import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000/anthropic",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)

reply = client.messages.create(
    model="deepseek-flash",
    max_tokens=16000,
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(next(block.text for block in reply.content if block.type == "text"))
```

## Claude Code

`privyx run claude` points Claude Code at the proxy's root, so Claude Code
calls `/v1/messages`, without DeepSeek's base path. Use
[gateway mode](../guide/proxy.md#gateway), which posts every request to one
fixed URL:

```yaml
# deepseek-claude.yaml
proxy:
  mode: gateway
  routes:
    /v1/messages: anthropic
provider:
  type: anthropic
  base_url: https://api.deepseek.com/anthropic/v1/messages
```

```bash
export PRIVYX_ANTHROPIC_API_KEY=...      # your DeepSeek key
privyx run -c deepseek-claude.yaml claude
```

In gateway mode Privyx sends its own key, the one above, and does not relay
what Claude Code presents. Claude Code still needs a credential of its own to
start.

## Good to know

- Model names and what each API accepts are DeepSeek's to define; see
  [their documentation](https://api-docs.deepseek.com/).
- With the route in place, check it: `privyx audit tail` shows
  `schema=anthropic` on `proxy.request` for a masked request.
