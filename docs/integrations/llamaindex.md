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

With `llama-index-llms-anthropic==0.13.0`, use Anthropic SDK 1.x:

```bash
pip install 'llama-index-llms-anthropic==0.13.0' 'anthropic>=1,<2'
```

SDK 2.8.0 changes `MessageParam` to a union type; this LlamaIndex version
tries to construct it and fails with `Cannot instantiate typing.Union`
before sending a request. The example below is verified with SDK 1.17.0.

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

`is_chat_model=True` selects chat completions. Without it, `OpenAILike` uses
the legacy completions API; Privyx masks text prompts on that path too.

## Good to know

- **Embeddings are masked.** Building an index sends your documents to
  `/v1/embeddings`, so the index holds vectors of the masked text. Use an
  anchor so a value gets the same token in the documents and in later
  queries; see [Embeddings](../guide/proxy.md#embeddings).
- **Retrieved context is masked.** What a query engine puts into the prompt
  goes through the chat endpoint, like any other message.
