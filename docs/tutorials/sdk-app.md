---
description: Put Privyx between your application and OpenAI or Anthropic. Chat, streaming, tool calls, and per-user sessions with the official Python and Node SDKs.
---

# An app on the OpenAI or Anthropic SDK

Your application puts user data into prompts: a support ticket, a customer
record, a log line. This tutorial routes those requests through Privyx, so the
provider receives placeholders and your code still gets real values back, in
plain replies, in streams, and in tool calls.

**You need:** Privyx [installed](../guide/installation.md), a provider API
key, and the OpenAI or Anthropic SDK for Python or Node. **Time:** fifteen
minutes.

## 1. Start the proxy

=== "OpenAI"

    ```bash
    privyx proxy --upstream https://api.openai.com
    ```

=== "Anthropic"

    ```bash
    privyx proxy --upstream https://api.anthropic.com
    ```

The proxy listens on `http://127.0.0.1:8000` and prints the upstream, the
paths it masks, and its engine settings. Leave it running.

## 2. Point the SDK at it

Only the base URL changes. The SDK still reads your key from the environment,
and Privyx relays it to the provider.

=== "OpenAI · Python"

    ```python
    from openai import OpenAI

    client = OpenAI(base_url="http://localhost:8000/v1")  # the key still comes from OPENAI_API_KEY

    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
    )
    print(reply.choices[0].message.content)
    ```

=== "OpenAI · Node"

    ```js
    import OpenAI from "openai";

    const client = new OpenAI({ baseURL: "http://localhost:8000/v1" }); // the key still comes from OPENAI_API_KEY

    const reply = await client.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "Write a short greeting to alice@example.com" }],
    });
    console.log(reply.choices[0].message.content);
    ```

=== "Anthropic · Python"

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

=== "Anthropic · Node"

    ```js
    import Anthropic from "@anthropic-ai/sdk";

    const client = new Anthropic({ baseURL: "http://localhost:8000" }); // the key still comes from ANTHROPIC_API_KEY

    const reply = await client.messages.create({
      model: "claude-opus-5-5",
      max_tokens: 16000,
      messages: [{ role: "user", content: "Write a short greeting to alice@example.com" }],
    });
    for (const block of reply.content) {
      if (block.type === "text") console.log(block.text);
    }
    ```

The model received `Write a short greeting to <PRIVYX_EMAIL_1>` and wrote its
answer around that token. Your program prints a greeting to
`alice@example.com`.

To leave the code untouched, set the base URL in the environment instead:
`OPENAI_BASE_URL=http://localhost:8000/v1` or
`ANTHROPIC_BASE_URL=http://localhost:8000`. The OpenAI URL ends in `/v1`; the
Anthropic one does not.

## 3. Stream the reply

Streaming needs no extra setting. Privyx restores the text as it arrives:

=== "OpenAI · Python"

    ```python
    from openai import OpenAI

    client = OpenAI(base_url="http://localhost:8000/v1")

    stream = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()
    ```

=== "Anthropic · Python"

    ```python
    import anthropic

    client = anthropic.Anthropic(base_url="http://localhost:8000")

    with client.messages.stream(
        model="claude-opus-5-5",
        max_tokens=64000,
        messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()
    ```

A provider may cut a token in two, `<PRIVYX_EMA` in one chunk and `IL_1>` in
the next. Privyx holds such a fragment back until the token is complete, so
your interface never shows half a placeholder. Text without tokens passes
through as it comes.

## 4. Tool calls

When the model calls one of your functions, the arguments it writes contain
tokens. Privyx restores them before your code sees the call:

=== "OpenAI · Python"

    ```python
    import json

    from openai import OpenAI

    client = OpenAI(base_url="http://localhost:8000/v1")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "Send an email to one recipient",
                "parameters": {
                    "type": "object",
                    "properties": {"to": {"type": "string"}},
                    "required": ["to"],
                },
            },
        }
    ]

    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Tell alice@example.com that the build is green"}],
        tools=tools,
    )
    call = reply.choices[0].message.tool_calls[0]
    print(call.function.name, json.loads(call.function.arguments))
    ```

=== "Anthropic · Python"

    ```python
    import anthropic

    client = anthropic.Anthropic(base_url="http://localhost:8000")

    tools = [
        {
            "name": "send_email",
            "description": "Send an email to one recipient",
            "input_schema": {
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        }
    ]

    reply = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": "Tell alice@example.com that the build is green"}],
        tools=tools,
    )
    call = next(block for block in reply.content if block.type == "tool_use")
    print(call.name, call.input)
    ```

```text
send_email {'to': 'alice@example.com'}
```

The model asked for `send_email` with `<PRIVYX_EMAIL_1>`; your function
receives the address. The result you send back in the next request is masked
again like any other message. In a streamed reply, Privyx collects a tool
call's arguments and delivers them restored in one piece, since a half-restored
JSON string would be of no use.

Tool and parameter *descriptions* are masked too. Tool *names*, `enum` values,
and other schema keywords are left as they are, because the provider validates
them.

## 5. The Responses API

OpenAI's Responses API is covered the same way:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1")

response = client.responses.create(
    model="gpt-4o-mini",
    input="Write a short greeting to alice@example.com",
)
print(response.output_text)
```

If you chain turns with `previous_response_id` and `store: true`, the earlier
turns live at the provider with tokens in them. Use a session, as in the next
step, so those tokens still mean the same values on the next turn.

## 6. One mapping per user or conversation

By default each request gets its own session: its tokens mean something only
for that one request and its reply, and the mapping is gone afterwards. For a
chat application that resends its whole history with every request, that is
enough: the history is masked again each time, with the same tokens in the
same places.

Choose the session yourself when the mapping has to outlive one request:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    default_headers={"x-privyx-session": "user-42"},
)
```

`anthropic.Anthropic` takes the same `default_headers` argument. Privyx
removes the header before it forwards the request.

For a session to survive a restart of the proxy, give it a persistent vault:

```yaml
# privyx.yaml
vault:
  type: sqlite               # needs privyx[sqlite]
  dsn: privyx.db
  ttl: 2592000               # forget a user's mapping after 30 days without a request
```

```bash
privyx proxy -c privyx.yaml --upstream https://api.openai.com
```

After two requests in that session, one naming `alice@example.com` and one
naming her and `bob@example.com`:

```console
$ privyx session list -c privyx.yaml
SESSION  LAST ACTIVE          CREATED              MAPPINGS
user-42  2026-10-02T14:38:38  2026-10-02T14:38:38  2

1 session(s) (vault: sqlite)

$ privyx session show -c privyx.yaml user-42
Session: user-42
  Vault:      sqlite
  Created:    2026-10-02T14:38:38
  Updated:    2026-10-02T14:38:38
  Mappings:   2

  <PRIVYX_EMAIL_1>                 → al*************om
  <PRIVYX_EMAIL_2>                 → bo***********om

  (values masked; pass --reveal to show them)
```

Alice kept `<PRIVYX_EMAIL_1>` in the second request, and Bob got the next
number.

!!! warning "Set the session in your backend"

    Whoever names a session can have its tokens restored. Take the id from
    the user your backend has authenticated, and never let a browser or an
    end user choose it. The vault holds original values, so protect it like
    the data it mirrors; see [Sessions and vault](../guide/sessions.md).

## 7. Keep the provider key in Privyx

So far each client sent its own key. To keep the key in one place, give it to
Privyx; it then replaces whatever key a client sends:

```bash
export PRIVYX_API_KEY=sk-...        # the real provider key
privyx proxy --upstream https://api.openai.com
```

Clients can now use any placeholder as their key. On Anthropic's paths
Privyx sends its key as `x-api-key`, the header that API expects.
[Configuration](../guide/configuration.md#provider) has the per-provider
variables and the order in which keys are chosen.

Now anyone who can reach the proxy spends your key. Keep the proxy on
`127.0.0.1` or behind something that authenticates callers; see
[Deployment](../guide/deployment.md).

## 8. Handle errors from Privyx

An error from the provider reaches your code unchanged. When Privyx itself
cannot complete a request, it answers with a status and an `error.type` that
starts with `privyx_`:

```python
import openai

try:
    reply = client.chat.completions.create(...)
except openai.APIStatusError as error:
    if (error.type or "").startswith("privyx_"):
        ...  # for example privyx_upstream_unreachable, with error.status_code 502
    raise
```

The request was not forwarded in most of these cases, which is the point: a
request that could not be masked does not leave. [Errors](../reference/errors.md)
lists every type and status.

## Check what happened

In the directory where the proxy runs:

```bash
privyx audit tail --no-follow      # the last exchanges, event by event
privyx audit stats --since 24h     # totals and masked entity types
```

Neither shows content. To see the masked text itself while you develop, use
`privyx detect --transform` on a sample of what your app sends.

## Next steps

- [Your own names and terms](custom-terms.md): the built-in patterns do not
  know your customers' names.
- [Integrations](../integrations/index.md): LangChain, LlamaIndex, LiteLLM, the
  Vercel AI SDK, and other providers.
- [A shared gateway for a team](team-gateway.md): one proxy with Redis, TLS,
  and an audit trail.
- [Limitations](../security/limitations.md): what is not masked, such as
  embeddings.
