---
description: Short answers to common questions about Privyx. What it is, what it sends where, how it affects answers and speed, and how to run it.
---

# FAQ

## The basics

### What does Privyx do, in one sentence?

It sits between your tool and an AI provider, replaces the sensitive values
it detects in each request with placeholders, and puts the original values
back into the reply.

### How is that different from redacting?

Redaction is one-way: once `ann@example.com` is `[REDACTED]`, the answer can
only say `[REDACTED]`. Privyx keeps the mapping, so the model works with
`<PRIVYX_EMAIL_1>` and you read `ann@example.com` in the answer, and a tool
the model calls receives the real address. One-way redaction is available
too, as the [`redact` operator](masking.md#redact).

### Does my data go through a Privyx server?

No. There is no Privyx service. The proxy is a program you run, on your own
machine or your own server, and it talks only to the upstream you configure.
It sends no telemetry.

### Do I need an account or an API key for Privyx?

No. You need whatever your provider requires. Privyx relays your client's key
to the provider, or holds a provider key for you if you prefer.

### What does it cost?

Nothing. Privyx is open source under the MIT license.

## Compatibility

### Which providers work?

Any provider that speaks one of three formats: OpenAI Chat Completions,
OpenAI Responses, or Anthropic Messages. That covers OpenAI and Anthropic and
the many services compatible with them. Gemini works through its
OpenAI-compatible endpoint; its native API is not masked yet. See
[Integrations](../integrations/index.md).

### Which clients work?

Any client that lets you set its base URL: the official SDKs, frameworks
built on them, `curl`, and coding tools. `privyx run` sets it up by itself for
Claude Code, Codex, and aider.

### Does streaming work? Tool calls? Reasoning?

Yes. A streamed reply is restored as it arrives, tool-call arguments are
restored before your code sees them, and reasoning text is restored like the
answer. See [An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md).

### Does Claude Code keep working with my subscription login?

Yes. In its default mode Privyx relays whatever credential the tool sends,
an API key or a login token alike.

### Can one proxy serve OpenAI and Anthropic at once?

One proxy forwards to one upstream. Run two, on different ports. A provider
that serves both formats on one host, as some routers do, needs only one.

## Privacy

### What is masked without any configuration?

Email addresses, phone numbers, credit card numbers, IP addresses, US social
security numbers, and secrets: API keys, tokens, JWTs, private keys,
passwords in URLs, and values assigned to secret-looking names. The list is
in [Detection](detection.md#built-in-patterns).

### Why was a name not masked?

A name has no shape a pattern could recognize. Add it to a
[word list](detection.md#word-lists), or add a detector that understands
language. [Your own names and terms](../tutorials/custom-terms.md) walks
through it.

### What does the provider still see?

Everything that was not detected, the kind of each masked value (the token
says `EMAIL`), and when two values are the same. See
[What the provider still learns](../security/limitations.md#what-the-provider-still-learns).

### Where are the original values kept?

In a session, inside the vault. By default the vault is the proxy's memory
and each request's session is dropped when the request ends. With a
persistent vault (SQLite or Redis) they are stored in plain text unless you
use the [`encrypt` operator](masking.md#encrypt). See
[Sessions and vault](sessions.md).

### Does Privyx log my prompts?

No. Its log never contains request or reply text at the default level, and
the audit trail records entity types and counts only. The one exception is a
debug log file you switch on yourself: `log_file` at `log_level: debug` can
hold original values.

### Does Privyx make me GDPR or HIPAA compliant?

Not by itself. It is a technical measure that reduces what a provider
receives. Compliance also depends on contracts, on what your detectors
actually catch, and on everything around the proxy.

### What does it not protect against?

Values no detector finds, API paths it does not mask, prompt injection, and
anyone who can reach an unprotected proxy. [Limitations](../security/limitations.md)
has the full list.

## Quality and speed

### Do answers get worse?

It depends on the task. The token tells the model what kind of value it
stands for, and the model can refer to it, repeat it, and pass it to a tool.
What it cannot do is reason about the value itself: it cannot tell you the
domain of a masked email address or add two masked numbers. If that matters, mask less
with the [`strict` policy](detection.md#policy), or use the
[`faker` operator](masking.md#faker), which shows the model a realistic
stand-in.

### How much latency does it add?

The built-in detection is pattern matching, measured in milliseconds. Every
request records its own cost: `transform_ms` on the `proxy.request` audit
event is the time spent masking, and the rest of `duration_ms` is mostly the
provider.

For orientation, `make bench` on the machine this page was written on took
about 2 ms to mask a 5 KB text, and about 140 ms for a 400 KB request seen
for the first time, 7 ms once its text was in the detection cache. Run it
yourself for numbers that mean something on your hardware. The `presidio`
and `llm` detectors cost more; the `llm` detector adds a model call.

### Does it break prompt caching?

No. Privyx masks a conversation the same way on every turn, so the part of
the request that did not change is byte-identical to the turn before and the
provider's cache still applies. With an [anchor](masking.md#anchors), which
`privyx run` sets up, that also holds across restarts.

### Is every turn of a long conversation scanned again?

No. Detection results are cached by text, so only new text is scanned.

## Running it

### Do I need a config file?

No. Without one, Privyx runs on built-in defaults plus `PRIVYX_*` environment
variables. A file is needed for word lists, patterns, and a few other
settings; `~/.privyx/config.yaml` is read on every run. See
[Configuration](configuration.md).

### Can I use it without the proxy?

Yes. `privyx mask` and `privyx unmask` work on text, files, and JSON
([tutorial](../tutorials/mask-files.md)), and the engine can run inside your
own Python program ([Python library](../development/python-library.md)).

### How do I change the word list without restarting by hand?

Start the proxy with `--reload`. It restarts itself when the config file
changes.

### Is it safe to expose the proxy on a network?

Not as it is: it has no authentication. See
[Deployment](deployment.md#who-can-reach-it).

### Is it production-ready?

It is in its `0.1.x` series. The masking and restoring paths are covered by
unit, integration, and property tests and by an end-to-end leak check with a
real coding agent; see [Testing](../development/testing.md). Defaults can
still change between releases, and the changelog says when they do.

## The project

### How do I report a bug or a leak?

A bug: [open an issue](https://github.com/ohp1x/privyx/issues). A case where
a detected value reaches the provider, or anything else security-related:
[report it privately](https://github.com/ohp1x/privyx/security/advisories/new).

### How can I contribute?

See [CONTRIBUTING.md](https://github.com/ohp1x/privyx/blob/main/CONTRIBUTING.md).
Detectors, operators, and vaults can also be added without changing Privyx,
as [plugins](../development/plugins.md).
