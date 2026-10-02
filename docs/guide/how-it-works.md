---
description: What Privyx does to a request and its reply, step by step, and the handful of terms the rest of the documentation uses.
---

# How it works

Privyx is an HTTP proxy. Your client sends its requests to Privyx instead of
to the AI provider, and Privyx forwards them. On the way out it replaces the
sensitive values it detects with placeholders; on the way back it puts the
original values into the reply.

![Your tool sends a request holding an email address and a password. Privyx forwards it with a placeholder in place of each and keeps the mapping in its session vault. The provider answers using the placeholder, and your tool gets the reply with the address back in it.](../assets/how-it-works.svg)

The provider works with `<PRIVYX_EMAIL_1>`. Your client never sees that
placeholder: it gets the address back. Nothing in the client changes except
the base URL it talks to.

## One request, step by step

1. **Your client calls Privyx.** It keeps its own API key, which Privyx
   relays to the provider, unless you give Privyx a key of its own.
2. **Privyx looks up the path.** `/v1/chat/completions`, `/v1/messages`, and
   `/v1/responses` are in its [routes](proxy.md#routes), so it knows where the
   text of such a request is: the messages, the system prompt, tool results,
   tool arguments. Settings such as `model` or `temperature` are left alone.
3. **It detects and masks.** The [detector](detection.md) finds sensitive
   values in each piece of text, the [policy](detection.md#policy) decides
   which of them to mask, and the [operator](masking.md) replaces each one,
   by default with a numbered token such as `<PRIVYX_EMAIL_1>`.
4. **It remembers the mapping.** Each token and the value it stands for go
   into a [session](sessions.md), which Privyx keeps in its vault. The
   provider never receives it.
5. **It forwards the request.** The provider receives the masked text and
   answers as usual. The model can reason about `<PRIVYX_EMAIL_1>`, repeat
   it, and pass it to a tool; it cannot read what is behind it.
6. **It restores the reply.** Every token the session issued is replaced by
   its original value: in the text, in reasoning, and in tool-call arguments.
   A streamed reply is restored as it arrives, also when a token is split
   between two chunks.
7. **It writes an audit event.** The [audit trail](../observability/audit-events.md)
   records which entity types were masked and how many, never the values.

If Privyx cannot mask a request, it does not forward it. A detector that
fails, a session vault that is down, or a body it cannot read ends in an
[error response](../reference/errors.md) from Privyx rather than in an
unmasked request.

## What is masked, and what is not

With no configuration, Privyx detects values with a recognizable shape: email
addresses, phone numbers, credit card numbers, IP addresses, US social
security numbers, and secrets such as API keys, tokens, private keys, and
passwords. The full list is in [Detection](detection.md#built-in-patterns).

A name, a company, or a project codename has no shape to recognize. For those
you give Privyx a [word list](detection.md#word-lists), or add a detector that
understands language ([Presidio](detection.md#presidio) or an
[LLM](detection.md#llm-detector)).

Only the chat requests in `proxy.routes` are masked. Any other path, such as
embeddings, is forwarded as the client sent it, unless you tell Privyx to
refuse those paths. [Limitations](../security/limitations.md) lists everything
Privyx does not cover.

## Three ways to use it

| Command | What it does | Use it when |
|---|---|---|
| [`privyx run TOOL`](cli.md#privyx-run) | Starts a proxy on a free port, launches the tool pointed at it, and stops the proxy when the tool exits | You use a coding agent such as Claude Code, Codex, or aider |
| [`privyx proxy`](cli.md#privyx-proxy) | Runs the proxy until you stop it | An application, several tools, or a team share one proxy |
| [`privyx mask`](cli.md#privyx-mask) / [`unmask`](cli.md#privyx-unmask) | Masks and restores a string, a file, or JSON, without a proxy | You prepare data in a script or a pipeline |

All three use the same engine and the same configuration, so
[`privyx detect`](cli.md#privyx-detect) shows on a sample text what any of
them would mask.

## The vocabulary

These terms come back throughout the documentation.

### Entity

One kind of sensitive value, named in upper case: `EMAIL`, `PHONE`, `API_KEY`,
or a name of your own such as `PROJECT`. The entity type is part of the token,
so the model still knows what kind of value it is looking at.

### Detector

The component that finds entities in text. `regex`, the default, uses the
built-in patterns plus your own patterns and word lists. `presidio` and `llm`
find names and other values that only make sense in context. Several
detectors can run together. See [Detection](detection.md).

### Policy

The filter between detection and masking. `default` masks everything the
detector found; `strict` masks only the entity types you list. See
[Policy](detection.md#policy).

### Operator

What a detected value becomes. `pseudonym`, the default, writes a token.
`hash`, `encrypt`, `faker`, and `redact` trade readability, reversibility,
and what the vault stores in different ways. See [Masking](masking.md#operators).

### Token

The placeholder that replaces a value, also called a pseudonym:
`<PRIVYX_EMAIL_1>` is the first email address a session saw. The syntax is
[configurable](masking.md#token-format).

### Session

The mapping from each token to the value it replaced. A request and its reply
share one session; whether the next request shares it too is the
[session strategy](sessions.md#which-session-a-request-uses).

### Vault

Where sessions are kept: in memory (the default), in a SQLite file, or in
Redis. See [Vault backends](sessions.md#vault-backends).

### Anchor

An optional secret that derives a token from the value itself, so the same
value gets the same token in every session and after a restart. See
[Anchors](masking.md#anchors).

### Route and wire schema

A route maps a request path to the JSON shape of its body: `openai` (Chat
Completions), `anthropic` (Messages), or `responses` (OpenAI Responses).
Privyx masks a request only when its path has a route. See
[Proxy modes and routes](proxy.md).

### Transparent and gateway mode

The two ways the proxy forwards a request. `transparent`, the default,
mirrors the client's path on the provider's host. `gateway` posts every
request to one fixed URL, for a provider whose endpoint sits behind a base
path. See [Two modes](proxy.md#two-modes).

### Audit trail

A log of what Privyx did, one JSON object per line: event names, entity
types, and counts, without any request or reply content. See
[Audit events and metrics](../observability/audit-events.md).

## Next steps

- [Quickstart](getting-started.md): run it for the first time.
- [Tutorials](../tutorials/index.md): one complete walkthrough per use case.
- [Threat model](../security/threat-model.md): what Privyx protects against.
