---
description: Route the Cline VS Code extension through Privyx with a custom base URL.
---

# Cline

Cline runs inside VS Code, so `privyx run` cannot launch it. Start the proxy
yourself and point Cline at it.

```bash
PRIVYX_SESSION_STRATEGY=conversation privyx proxy --upstream https://api.anthropic.com
```

`conversation` keeps one session per conversation, so a value gets the same
placeholder on every turn. Keep the terminal open while you use Cline.

## Anthropic models

In Cline's settings, choose the **Anthropic** API provider, enter your key,
tick **Use custom base URL**, and enter:

```text
http://localhost:8000
```

## OpenAI and OpenAI-compatible models

Start the proxy with that provider's URL as `--upstream` (for example
`https://api.openai.com`), choose the **OpenAI Compatible** API provider, and
set its **Base URL** to:

```text
http://localhost:8000/v1
```

## Check it

Ask Cline something that names an email address, then run
`privyx audit tail`: a `session.transform` event for the request shows that
Privyx masked it. A provider Cline signs in to on its own (its own account,
a browser login) does not go through Privyx.
