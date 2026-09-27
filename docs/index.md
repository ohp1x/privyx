# Privyx

> AI data privacy gateway — a privacy engine + proxy for LLM providers.

Privyx sits between your application and an AI provider. It pseudonymizes
sensitive data (PII) in each request, keeps the mapping in a session vault, and
restores the original values in the reply — batch or streaming — without
changing your application code.

## Install

```bash
pip install privyx
```

Or run the published image: see [Docker](docker.md).

## Quick start

```bash
# Start the drop-in transparent proxy
privyx proxy --upstream https://api.openai.com
```

Then point any client at Privyx — no code change, just the base URL:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1      # OpenAI clients
export ANTHROPIC_BASE_URL=http://localhost:8000      # Anthropic clients
```

## Where to go next

- [Getting started](guide/getting-started.md) — install, first proxy, `privyx run`
- [Examples](guide/examples.md) — OpenAI and Anthropic SDKs, curl, coding tools, config recipes
- [Configuration](guide/configuration.md) — the config file, environment variables, and every setting
- [Detection](guide/detection.md) — built-in patterns, secrets, word lists, Presidio, the LLM detector
- [Masking](guide/masking.md) — operators, token format, anchors
- [Sessions and vault](guide/sessions.md) — session strategies, vault backends, expiry
- [CLI reference](guide/cli.md) — every command and option
- [Proxy architecture](architecture/proxy.md) — transparent and gateway modes, routes, streaming
- [Threat model](security/threat-model.md) — what Privyx protects against, and what it does not
