---
description: Route Continue (VS Code, JetBrains, and its CLI) through Privyx with apiBase.
---

# Continue

Continue reads its models from `~/.continue/config.yaml`. Start the proxy, then
give each model that should go through Privyx an `apiBase`.

```bash
PRIVYX_SESSION_STRATEGY=conversation privyx proxy --upstream https://api.openai.com
```

`conversation` keeps one session per conversation, so a value gets the same
placeholder on every turn. Keep the terminal open while you use Continue.

## OpenAI and OpenAI-compatible models

```yaml title="~/.continue/config.yaml"
models:
  - name: GPT through Privyx
    provider: openai
    model: gpt-4o-mini
    apiBase: http://localhost:8000/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
```

## Anthropic models

Start the proxy with `--upstream https://api.anthropic.com`. Continue's
Anthropic provider expects a base URL ending in `/v1/`:

```yaml title="~/.continue/config.yaml"
models:
  - name: Claude through Privyx
    provider: anthropic
    model: claude-opus-5-5
    apiBase: http://localhost:8000/v1/
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
```

One proxy has one upstream: for models of two providers, run two proxies on
two ports (`--port 8001`) and give each model its own `apiBase`. A model without
`apiBase` goes to its provider directly. Autocomplete and embeddings models
are models too: give them the same `apiBase` if their text should be masked
(Privyx masks `/v1/embeddings` and `/v1/completions` as well).

Privyx does not edit `config.yaml` for you: the extension keeps running after
any one command would end, and a proxy URL left in the file would point at a
closed port.
