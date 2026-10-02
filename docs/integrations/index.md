---
description: How to connect Privyx to providers, SDKs, frameworks, and coding tools. One rule covers most of them: set the client's base URL.
---

# Integrations

Privyx works with anything that lets you set the address of its AI provider.
The client keeps its code and its key; it talks to Privyx, and Privyx talks to
the provider.

Two base URLs cover most clients:

| The client speaks | Base URL | Start Privyx with |
|---|---|---|
| OpenAI format (Chat Completions, Responses) | `http://localhost:8000/v1` | `privyx proxy --upstream https://api.openai.com` |
| Anthropic format (Messages) | `http://localhost:8000` | `privyx proxy --upstream https://api.anthropic.com` |

## Coding tools

| Tool | How | More |
|---|---|---|
| Claude Code | `privyx run claude` | [Coding agents](../tutorials/coding-agents.md) |
| Codex | `privyx run codex` | [Coding agents](../tutorials/coding-agents.md) |
| aider | `privyx run aider` | [Coding agents](../tutorials/coding-agents.md) |
| Any tool with a base-URL variable | `privyx run --env-var NAME -u URL -- tool` | [`privyx run`](../guide/cli.md#privyx-run) |

## SDKs and frameworks

| Client | Setting | Page |
|---|---|---|
| OpenAI SDK (Python, Node) | `base_url` / `baseURL`, or `OPENAI_BASE_URL` | [An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md) |
| Anthropic SDK (Python, Node) | `base_url` / `baseURL`, or `ANTHROPIC_BASE_URL` | [An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md) |
| LangChain | `base_url` on `ChatOpenAI` / `ChatAnthropic` | [LangChain](langchain.md) |
| LlamaIndex | `api_base` on `OpenAI`, `base_url` on `Anthropic` | [LlamaIndex](llamaindex.md) |
| LiteLLM | `api_base` | [LiteLLM](litellm.md) |
| Vercel AI SDK | `baseURL` on `createOpenAI` / `createAnthropic` | [Vercel AI SDK](vercel-ai-sdk.md) |
| `curl` and plain HTTP | The URL | [Examples](../guide/examples.md#curl) |

## Providers

| Provider | Mode | Needs | Page |
|---|---|---|---|
| OpenAI | transparent | nothing | [OpenAI](../providers/openai.md) |
| Anthropic | transparent | nothing | [Anthropic](../providers/anthropic.md) |
| Google Gemini | transparent or gateway | a route for its OpenAI-compatible path | [Google Gemini](../providers/google.md) |
| Azure OpenAI | transparent | a route per path | [Azure OpenAI](../providers/azure-openai.md) |
| OpenRouter | transparent | a route | [OpenRouter](../providers/openrouter.md) |
| DeepSeek | transparent, or gateway for Claude Code | a route for its Anthropic format | [DeepSeek](../providers/deepseek.md) |
| Ollama and local servers | transparent | nothing | [Ollama and local servers](../providers/local.md) |
| Anything else compatible | either | see the page | [Any other endpoint](../providers/generic.md) |

"A route" means a line in `proxy.routes` that tells Privyx to mask a path it
does not know by default. Without it, that path is forwarded unmasked; see
[Routes](../guide/proxy.md#routes).

## What is covered everywhere

Whatever the client, Privyx masks chat requests in the three formats it
knows and restores their replies, streamed or not, including tool-call
arguments. It does not mask embeddings, images, or other API paths; see
[Limitations](../security/limitations.md).

## Check an integration

After the first request through a new client, look at the audit trail:

```bash
privyx audit tail --no-follow
```

A masked request shows a `proxy.request` line with `schema=openai`,
`schema=anthropic`, or `schema=responses`. If that line is missing, the client
did not go through Privyx; if it shows `schema=null`, the path has no route
and was forwarded as sent.
