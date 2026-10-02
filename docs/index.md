---
description: Privyx is a proxy that masks secrets and personal data in requests to AI providers and restores them in the reply, without changes to your client.
hide:
  - navigation
  - toc
---

# Privyx

**Keep secrets and personal data out of LLM prompts.**

Privyx is a proxy between your tools and an AI provider. It replaces the API
keys, passwords, email addresses, and other sensitive values it detects in a
request with placeholders, and puts the originals back into the reply. The
only thing that changes in your client is its base URL.

[Get started](guide/getting-started.md){ .md-button .md-button--primary }
[How it works](guide/how-it-works.md){ .md-button }

![A terminal: privyx mask replaces a secret key and an email address in a prompt with tokens, and privyx unmask puts the email address back into the model's reply.](assets/demo.gif)

## Try it

=== "A coding agent"

    ```bash
    pip install privyx
    privyx run claude        # or: codex, aider
    ```

    Claude Code starts as usual, with its traffic going through Privyx.

=== "An OpenAI or Anthropic client"

    ```bash
    pip install privyx
    privyx proxy --upstream https://api.openai.com
    ```

    ```bash
    export OPENAI_BASE_URL=http://localhost:8000/v1
    ```

=== "Without installing"

    ```bash
    uvx privyx detect --transform "DB_PASSWORD=hunter2, mail dana@acme.example"
    ```

    This only shows what would be masked; nothing is sent anywhere.

The [Quickstart](guide/getting-started.md) walks through each of these.

## What the provider sees

![Your tool sends a request holding an email address and a password. Privyx forwards it with a placeholder in place of each and keeps the mapping in its session vault. The provider answers using the placeholder, and your tool gets the reply with the address back in it.](assets/how-it-works.svg)

The model works with `<PRIVYX_EMAIL_1>`: it can reason about it, repeat it,
and hand it to a tool. The mapping back to the real value stays with Privyx.

## Pick your path

<div class="grid cards" markdown>

-   **I use a coding agent**

    ---

    Claude Code, Codex, or aider reads your `.env`, your logs, and your git
    history. Run it through Privyx with one command.

    [Coding agents](tutorials/coding-agents.md)

-   **I am building an application**

    ---

    Keep your users' data out of the prompts your app sends, with the OpenAI
    or Anthropic SDK or a framework on top of them.

    [An app on the OpenAI or Anthropic SDK](tutorials/sdk-app.md)

-   **I run a gateway for a team**

    ---

    One proxy for everyone: a shared vault, TLS, an audit trail, and metrics.

    [A shared gateway for a team](tutorials/team-gateway.md)

-   **I want to mask files**

    ---

    Logs, JSON, and datasets, masked and restored in a script or in CI,
    without a proxy.

    [Files and logs in a pipeline](tutorials/mask-files.md)

-   **I need my own names masked**

    ---

    People, customers, and codenames have no pattern to recognize. Teach
    Privyx yours with word lists, patterns, or a detector that reads language.

    [Your own names and terms](tutorials/custom-terms.md)

-   **I need detection with code**

    ---

    A checksum, a lookup, a format no pattern can describe: write a detector
    plugin in a few lines of Python.

    [A detector plugin](tutorials/detector-plugin.md)

</div>

## What it does

- **Masks secrets and personal data.** Built-in patterns cover email
  addresses, phone numbers, card numbers, IP addresses, API keys, tokens,
  private keys, and passwords. Add your own [word lists and patterns](guide/detection.md),
  or a detector that understands names: Presidio or an LLM.
- **Restores the reply.** In batch and streaming responses, in reasoning text,
  and in [tool-call arguments](tutorials/sdk-app.md#4-tool-calls), so your tools
  receive real values.
- **Drops in.** It speaks OpenAI Chat Completions and Responses and Anthropic
  Messages, so [SDKs, frameworks, and coding tools](integrations/index.md)
  only need a base URL.
- **Wraps coding agents.** [`privyx run`](guide/cli.md#privyx-run) starts a
  proxy, launches the tool through it, and cleans up afterwards.
- **Keeps sessions your way.** One mapping per request, per client, or per
  conversation, [in memory, SQLite, or Redis](guide/sessions.md).
- **Shows what it did.** A PII-safe [audit trail](observability/audit-events.md)
  and Prometheus metrics count what was masked, without recording any of it.
- **Fails closed.** A request it cannot mask is [not forwarded](reference/errors.md).
- **Extends.** [Plugins](development/plugins.md) add detectors, operators,
  policies, vaults, and providers.

## What it does not do

Privyx reduces what a provider sees. It does not make a prompt safe by itself:

- Names, organizations, and project terms are only masked once you configure
  a word list or a detector for them.
- Only chat requests are masked. Other API paths, such as embeddings, are
  forwarded as the client sent them, unless you tell Privyx to refuse them.
- Images, audio, and other binary content are not inspected.
- The proxy has no authentication of its own.

[Limitations](security/limitations.md) has the full list, and the
[threat model](security/threat-model.md) says what Privyx protects against.

## Project

Privyx is open source under the MIT license, on
[GitHub](https://github.com/ohp1x/privyx) and
[PyPI](https://pypi.org/project/privyx/). It is in its `0.1.x` series; the
[changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md) lists
what each release changed. Questions and ideas are welcome in
[Discussions](https://github.com/ohp1x/privyx/discussions), and security
issues go through a [private report](https://github.com/ohp1x/privyx/security/advisories/new).
