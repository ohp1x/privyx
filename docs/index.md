# Privyx

> AI data privacy gateway — a privacy engine + proxy for LLM providers.

Privyx sits between your application and an AI provider. It pseudonymizes
sensitive data (PII) in each request, keeps the mapping in a session vault, and
restores the original values in the reply — batch or streaming — without
changing your application code.

## Install

```bash
pip install privyx
# or
uv add privyx
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

- [Proxy](architecture/proxy.md) — transparent and gateway modes, routes, sessions
- [Privacy engine](architecture/privacy-engine.md) — detectors, policies, operators, anchors
- [Providers](providers/openai.md) — OpenAI, Anthropic, Google Gemini, and any OpenAI-compatible API
- [Threat model](security/threat-model.md) — what Privyx protects against, and what it does not
- [Audit events](observability/audit-events.md) — the PII-safe audit trail and `/metrics`
- [Plugins](development/plugins.md) — custom detectors, operators, and providers

The [README](https://github.com/ohp1x/privyx#readme) covers the CLI and
configuration in more detail.
