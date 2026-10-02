---
description: Use LlamaIndex LLMs through Privyx. OpenAI, Anthropic, and any OpenAI-compatible model, with the one environment variable to watch out for.
---

# LlamaIndex

LlamaIndex LLM classes accept a base URL. Point it at Privyx and the prompts
LlamaIndex builds, including retrieved context, are masked before they reach
the provider.

## OpenAI models

```bash
privyx proxy --upstream https://api.openai.com
```

```python
from llama_index.llms.openai import OpenAI

llm = OpenAI(model="gpt-4o-mini", api_base="http://localhost:8000/v1")

print(llm.complete("Write a short greeting to alice@example.com"))
```

!!! warning "`OPENAI_BASE_URL` is not enough"

    LlamaIndex's `OpenAI` class reads `OPENAI_API_BASE`. With only
    `OPENAI_BASE_URL` set, it calls the provider directly and nothing is
    masked. Pass `api_base`, or set `OPENAI_API_BASE`.

## Anthropic models

```bash
privyx proxy --upstream https://api.anthropic.com
```

```python
from llama_index.llms.anthropic import Anthropic

llm = Anthropic(model="claude-opus-5-5", base_url="http://localhost:8000")

print(llm.complete("Write a short greeting to alice@example.com"))
```

## Any OpenAI-compatible model

For a model name the `OpenAI` class does not know, use `OpenAILike`:

```python
from llama_index.llms.openai_like import OpenAILike

llm = OpenAILike(
    model="my-model",
    api_base="http://localhost:8000/v1",
    api_key="unused",
    is_chat_model=True,
)

print(llm.complete("Write a short greeting to alice@example.com"))
```

`is_chat_model=True` matters: without it, `OpenAILike` uses the legacy
completions API, a path Privyx does not mask.

## Good to know

- **Embeddings are not masked.** Building an index sends your documents to
  `/v1/embeddings`, which Privyx forwards as sent. Mask the documents first
  with [`privyx mask`](../tutorials/mask-files.md), embed with a local model,
  or refuse the path with
  [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route).
- **Retrieved context is masked.** What a query engine puts into the prompt
  goes through the chat endpoint, like any other message.
