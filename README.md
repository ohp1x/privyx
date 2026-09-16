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

# Start the drop-in transparent proxy (default mode)
privyx proxy --upstream https://api.openai.com

# Run a provider through the proxy
privyx run openai
```

Then point any client at Privyx — no code change, just the base URL:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1      # OpenAI clients
export ANTHROPIC_BASE_URL=http://localhost:8000      # Anthropic clients
```

Chat requests are pseudonymized before forwarding and the reply is restored on
the way back (batch or streaming, including tool-call arguments); every other
path is forwarded verbatim. Use `--gateway` for the narrow chat-only app.

### Pseudonymizing your own terms

Beyond the built-in patterns, `detector.terms` takes plain word lists — Privyx
escapes them and matches case-insensitively, longest term first:

```yaml
# my.yaml
detector:
  type: regex                 # keeps the built-in EMAIL/PHONE/CREDIT_CARD/IP_ADDRESS/SSN
  terms:
    PERSON:       [ann, bob]
    ORGANIZATION: [acme, initech]
    URL:          ["https://git.internal.example/team"]
```

```bash
privyx detect -c my.yaml --transform "ann at acme"   # → <PRIVYX_PERSON_1> at <PRIVYX_ORGANIZATION_2>
privyx proxy -c my.yaml --upstream https://api.openai.com
```

The entity name is yours to choose — it becomes the `{type}` in the token.
`terms` and `patterns` can both be set; see `configs/examples/terms.yaml`.

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
