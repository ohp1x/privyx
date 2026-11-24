---
description: "Use the Anthropic API through Privyx: setup, keys, and what is masked and restored in Messages requests, streams, thinking, and tool calls."
---

# Anthropic

Point the Anthropic SDK at Privyx. In the default transparent mode,
`/v1/messages` and `/v1/messages/count_tokens`, which takes the same body, are
masked, and the client's `x-api-key` and `anthropic-version` headers go
upstream.

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

Start it with:

```bash
privyx proxy --upstream https://api.anthropic.com
```

The base URL has no `/v1`: the SDK adds `/v1/messages` itself. Streaming and
tool calls are shown in
[An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md), and Claude
Code in [Coding agents](../tutorials/coding-agents.md).

## Configuration

Transparent mode needs no config file. `provider: anthropic` selects the
built-in provider `anthropic`, which makes Anthropic the upstream when
`--upstream` is not given, sends a key held by Privyx as `x-api-key`, and adds
`anthropic-version: 2023-06-01` when the client sends none. Written out, with
its default:

```yaml
provider: anthropic
providers:
  anthropic:
    type: anthropic
    base_url: https://api.anthropic.com/v1/messages
```

Credential settings can read environment variables, such as
`api_key: ${ANTHROPIC_API_KEY}`. A missing variable stops startup; see
[Configuration](../guide/configuration.md#where-settings-come-from).

Privyx's own key is, in both modes, the `api_key` of the provider in use;
`PRIVYX_ANTHROPIC_API_KEY` sets it for the built-in provider `anthropic`,
which sends it to `api.anthropic.com` only. When one is set it replaces the
client's key on every request; when none is, the transparent proxy relays the
client's own `x-api-key` header and the gateway sends none.

## What is covered

`system` (a string or text blocks) and every string in `messages` are masked,
except under keys that hold no prose: ids, `type`, `name`, `signature`, base64
`data`, URLs, `media_type`, and `cache_control`; the full rule is in
[Proxy architecture](../architecture/proxy.md#what-gets-transformed-and-restored).
That covers, among others, `text` and `thinking`, the `input` of `tool_use`
and `server_tool_use` (every value, whatever its key), `tool_result` content,
`document` blocks (a plain-text `source.data`, `source.content`, `title`,
`context`), `search_result` (`title`, `content[].text`),
`citations[].cited_text`, and code-execution results (`stdout`, `stderr`). In
`tools`, only the tool and parameter descriptions are masked; `tool_choice`,
`metadata`, and the rest of the top level are left alone. In a batch reply,
every string is restored.

## Streaming, thinking, and tools

In a stream, the text and the thinking of `content_block_delta` events
(`delta.text`, `delta.thinking`) are each restored separately as they arrive.
The partial JSON of a `tool_use` or `server_tool_use` block is collected until
`content_block_stop` and delivered restored in one event. Every other event,
such as `citations_delta` (a complete `cited_text`), a `content_block_start`
that carries a whole server-tool result, or `message_start`, has each of its
strings restored. `signature` values are never changed, and `message_stop` is
sent last, so nothing arrives after it.

The thinking text a signature covers is remembered as the upstream sent it.
When the client sends that block back, Privyx forwards the remembered text,
byte for byte, rather than masking the restored text again, so the signature
still matches
([Proxy architecture](../architecture/proxy.md#what-gets-transformed-and-restored)).
