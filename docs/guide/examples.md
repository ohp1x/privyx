---
description: Copy-and-paste examples for Privyx. SDK snippets, curl, coding tools, configuration recipes, and command-line one-liners.
---

# Examples

Short recipes to copy. Each client below talks to Privyx instead of the
provider: only the base URL changes, and the client keeps its own API key,
which Privyx relays. The examples assume a proxy on the default port:

```bash
privyx proxy --upstream https://api.openai.com        # for the OpenAI examples
privyx proxy --upstream https://api.anthropic.com     # for the Anthropic examples
```

For a guided walkthrough instead, see the [tutorials](../tutorials/index.md).

## OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1")  # the key still comes from OPENAI_API_KEY

reply = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

The provider receives `<PRIVYX_EMAIL_1>` in place of the address; the reply
has the address back. Streaming (`stream=True`) is restored as it arrives.

Setting `OPENAI_BASE_URL=http://localhost:8000/v1` has the same effect without
touching the code.

## Anthropic Python SDK

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

Or set `ANTHROPIC_BASE_URL=http://localhost:8000`.

## Node

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8000/v1" }); // the key still comes from OPENAI_API_KEY

const reply = await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Write a short greeting to alice@example.com" }],
});
console.log(reply.choices[0].message.content);
```

The Anthropic SDK for Node takes `baseURL: "http://localhost:8000"` in the
same way. Streaming, tool calls, and the Responses API are in
[An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md); LangChain,
LlamaIndex, LiteLLM, and the Vercel AI SDK are under
[Integrations](../integrations/index.md).

## One session per user

By default every request gets its own session. To keep one mapping per user
of your application, send an `x-privyx-session` header:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    default_headers={"x-privyx-session": "user-42"},
)
```

`anthropic.Anthropic` takes the same `default_headers` argument. Privyx strips
the header before forwarding and returns the session id in the response's
`x-privyx-session` header. A persistent vault keeps the mapping across
restarts; see [Sessions and vault](sessions.md).

## curl

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini",
       "messages": [{"role": "user", "content": "Write a short greeting to alice@example.com"}]}'
```

```bash
curl http://localhost:8000/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-5-5", "max_tokens": 1024,
       "messages": [{"role": "user", "content": "Write a short greeting to alice@example.com"}]}'
```

Privyx adds the `anthropic-version` header when the request has none.

## Coding tools

`privyx run` starts a proxy for the tool and points it there:

```bash
privyx run claude      # Claude Code
privyx run codex       # Codex
privyx run aider       # aider
```

For a long-running proxy that several terminals share, start it once and set
the variable yourself:

```bash
privyx proxy -c privyx.yaml --upstream https://api.anthropic.com
ANTHROPIC_BASE_URL=http://localhost:8000 claude
```

## On the command line

```bash
# What would be masked in this text, and what the provider would get
privyx detect --transform "DB_PASSWORD=hunter2, mail dana@acme.example"

# The same for a file
privyx detect --transform --stdin < app.log

# Mask a file and keep the mapping; restore the answer later
privyx mask --map map.json -i app.log -o app.masked.log
privyx unmask --map map.json -i answer.txt

# Only the messages of a JSON request
privyx mask --map map.json -i request.json --path '$.messages'

# A JSONL dataset through a pipe
cat tickets.jsonl | privyx mask --map map.json -f jsonl -i - -o tickets.masked.jsonl

# What happened lately, without any content
privyx audit stats --since 24h
```

## Configuration recipes

These files ship in the repository's
[`configs/`](https://github.com/ohp1x/privyx/tree/main/configs) folder and in the
Docker image under `/app/configs`.

### Your own word lists

```yaml
--8<-- "configs/examples/terms.yaml"
```

### No plain text in the vault

```yaml
--8<-- "configs/examples/encrypt.yaml"
```

### A stricter deployment

```yaml
--8<-- "configs/strict.yaml"
```

### Sessions that survive a restart

```yaml
vault:
  type: sqlite               # needs privyx[sqlite]
  dsn: /var/lib/privyx/privyx.db
  ttl: 604800                # forget sessions idle for a week

session:
  strategy: conversation     # one session per conversation
```

### Refuse what cannot be masked

```yaml
proxy:
  passthrough_unknown: false   # 403 for embeddings and every other unrouted path
```

### Names without a list

```yaml
detector:
  - type: regex               # built-in patterns, your terms and patterns
  - type: presidio            # needs privyx[presidio] and a spaCy model
    language: en
    entities: [PERSON, LOCATION]
```

## Using the engine from Python

The engine can run inside your own program, without the proxy. The scripts in
[`examples/`](https://github.com/ohp1x/privyx/tree/main/examples) show how,
and the test suite runs each of them. [Python library](../development/python-library.md)
walks through them.

```python
--8<-- "examples/from_config.py"
```
