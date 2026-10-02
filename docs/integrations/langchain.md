---
description: Use LangChain's ChatOpenAI and ChatAnthropic through Privyx by setting the base URL.
---

# LangChain

LangChain's chat models call the provider through the official SDKs, so they
take a base URL. Point it at Privyx and every prompt, chain, and agent step
is masked on the way out and restored on the way back.

## OpenAI models

```bash
privyx proxy --upstream https://api.openai.com
```

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini", base_url="http://localhost:8000/v1")

print(llm.invoke("Write a short greeting to alice@example.com").content)
```

The model receives `<PRIVYX_EMAIL_1>`; `content` has the address back.
Streaming works the same way:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini", base_url="http://localhost:8000/v1")

for chunk in llm.stream("Write a short greeting to alice@example.com"):
    print(chunk.content, end="", flush=True)
print()
```

## Anthropic models

```bash
privyx proxy --upstream https://api.anthropic.com
```

```python
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-opus-5-5", base_url="http://localhost:8000")

print(llm.invoke("Write a short greeting to alice@example.com").content)
```

The Anthropic base URL has no `/v1`.

## Without changing code

Both classes read the SDK's environment variable when no `base_url` is
given:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
export ANTHROPIC_BASE_URL=http://localhost:8000
```

## Good to know

- **Tools and agents.** Tool-call arguments are restored before LangChain runs
  the tool, so your tools receive real values, and their results are masked
  again on the next request.
- **Embeddings are not masked.** `OpenAIEmbeddings` calls `/v1/embeddings`,
  which Privyx forwards as sent. If a vector store must not hold original
  values at the provider, mask the text first with
  [`privyx mask`](../tutorials/mask-files.md), or refuse the path with
  [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route).
- **One mapping per conversation.** Pass a session id with
  `default_headers={"x-privyx-session": "..."}`; see
  [One mapping per user or conversation](../tutorials/sdk-app.md#6-one-mapping-per-user-or-conversation).
