# Privyx

> AI data privacy gateway — a privacy engine + proxy for LLM providers.

Privyx is a modular, extensible privacy gateway that intercepts traffic to/from AI
providers, pseudonymizes sensitive data (PII), manages session vaults, and
deanonymizes streaming responses — all without changing your application code.

## Features

- **Privacy Engine** — pluggable detectors, policies, operators, and anchors
- **Transparent Proxy** — works with OpenAI, Anthropic, and any OpenAI-compatible HTTP provider
- **Streaming Deanonymization** — real-time pseudonym reversal in SSE streams
- **Session Vault** — memory, SQLite, or Redis backends
- **CLI** — `privyx proxy`, `privyx run`, `privyx detect`, `privyx inspect`, `privyx doctor`
- **Plugin System** — custom detectors, operators, and providers

## Quick Start

```bash
# Install
uv sync --all-extras

# Start the proxy
privyx proxy

# Run a provider through the proxy
privyx run openai
```

## Installation

```bash
pip install privyx
# or
uv add privyx
```

## Documentation

See [docs/](docs/) for architecture, provider guides, and security documentation.

## License

MIT
