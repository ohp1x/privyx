# Examples

Each client below talks to Privyx instead of the provider. Only the base URL
changes; the client keeps its own API key, which Privyx relays upstream. The
examples assume a proxy on the default port:

```bash
privyx proxy --upstream https://api.openai.com        # for the OpenAI examples
privyx proxy --upstream https://api.anthropic.com     # for the Anthropic examples
```

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

client = anthropic.Anthropic(base_url="http://localhost:8000")  # key from ANTHROPIC_API_KEY

reply = client.messages.create(
    model="claude-opus-5",
    max_tokens=16000,
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(next(block.text for block in reply.content if block.type == "text"))
```

Or set `ANTHROPIC_BASE_URL=http://localhost:8000`.

## One session per user

By default every request gets its own session. To keep one pseudonym mapping
per user of your application, send an `x-privyx-session` header:

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

## Using the engine from Python

The engine can run inside your own program, without the proxy. These scripts
live in [`examples/`](https://github.com/ohp1x/privyx/tree/main/examples) and
the test suite runs each of them.

### Mask and restore a string

```python
--8<-- "examples/basic.py"
```

### Serve the gateway from your own code

```python
--8<-- "examples/fastapi_gateway.py"
```
