---
description: What Privyx does not mask, does not restore, and does not protect against, with what you can do about each.
---

# Limitations

Privyx reduces what an AI provider sees of your data. It does not make a
prompt safe by itself, so it pays to know where it stops. This page lists the
limits in one place; the [threat model](threat-model.md) explains
the reasoning behind them.

## Detection

**Only what a detector finds is masked.** Everything else in a request reaches
the provider as you wrote it.

| Limit | What to do |
|---|---|
| Names, organizations, and project terms are not found without configuration. The built-in patterns recognize values by their shape, and a name has none. | List them as [word lists](../guide/detection.md#word-lists), or add the [Presidio](../guide/detection.md#presidio) or [LLM](../guide/detection.md#llm-detector) detector. |
| No detector is complete. A pattern can miss a format it was not written for; a language model can overlook a name. | Test with [`privyx detect`](../guide/cli.md#privyx-detect) on real samples of what you send. The test suite scores the built-in patterns on a labeled corpus; see [Testing](../development/testing.md#detection-corpus). |
| Country-specific formats are not all enabled by default. | Enable the supported [national ID detectors](../guide/detection.md#opt-in-detectors), or add a pattern or plugin for another format. |
| Encoding or splitting a value can hide it from the default scan. | Opt into [`decode`](../guide/detection.md#hidden-values) for supported encodings and string concatenations, or mask before encoding. Values spread across fields or encoded in another way can still be missed. |
| Meaning is not masked. "The patient in room 4 who was admitted on Monday" names nobody and still identifies someone. | Keep such text out of the prompt; no placeholder can stand in for it. |

## Coverage

**Only requests on masking routes are inspected**, including chat, legacy
completions, embeddings, and supported token-counting endpoints. Within a
request, only the fields covered by its wire schema are scanned.

| Limit | What to do |
|---|---|
| A path without a [route](../guide/proxy.md#routes) is forwarded as the client sent it: batch and file uploads, reads of stored objects. | Set [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route) to refuse those paths, or mask the data first with `privyx mask`. |
| Images, audio, PDFs, and other binary or base64 content are not inspected. | Do not rely on Privyx for files; extract and mask the text yourself. |
| Request settings are left as sent: `model`, `metadata`, `user`, tool names, parameter names, `enum` values, and a `response_format` schema. | Keep sensitive values out of those fields. Tool and parameter descriptions are masked. |
| URLs and ids inside messages are skipped: an image URL with a name in its query string goes out unchanged. | Put such values in the text of a message instead. |
| JSON keys are never rewritten, and a chat message's participant `name` is kept. | Put the value, not the key, where the sensitive text is. |
| Request headers are not masked, with one exception for Codex's turn metadata. | Do not send sensitive values in custom headers. |
| A WebSocket connection is refused with `426`, since Privyx could not mask its frames. | Clients fall back to HTTP; Codex does so by itself. |
| Privyx sees only the traffic a tool sends to its provider's base URL. Telemetry or other requests to different hosts do not pass through it, and a tool that ignores its base URL setting bypasses Privyx entirely. | `privyx run` warns when no request took a masking route, even if other requests reached the proxy; see [Check that it works](../tutorials/coding-agents.md#3-check-that-it-works). |
| Exact signed thinking remembered from an upstream response is reinstated after DLP checks the masked request, so model-written original values in that text may return to the provider. | Only the remembered text is exempt; other fields remain checked. Unknown or evicted signatures get normal masking. See [Leak check](../guide/masking.md#leak-check). |

## Restoring

| Limit | What to do |
|---|---|
| A token the model rewrote is not restored: translated, split by a space, or wrapped in other characters. A few departures are tolerated: a token missing one or both outer delimiters, a token in another case, and a long id on its own, each only for a token of the session. | Pick a [token format](../guide/masking.md#token-format) your model reproduces reliably. |
| `logprobs` token strings are not restored, so a client that asks for them can see fragments of a token. | Do not request `logprobs` through Privyx. |
| The `redact` operator cannot be restored at all, and `faker`, `fpe`, and `partial` restore by matching their substitutes, which the model may also write by coincidence. | Use `pseudonym` when exact reversal matters. |
| A tool that the provider runs for the model, such as web search or code execution, receives tokens. A search for a masked name searches for the token. | Leave unmasked what such a tool must see, with the [`strict` policy](../guide/detection.md#policy). |
| The model cannot reason about what it cannot see. It can pass a masked email address along; it cannot tell you its domain. | Mask less with the `strict` policy, or use the [`faker`](../guide/masking.md#faker) operator so the model sees a plausible stand-in, or [`partial`](../guide/masking.md#partial) so it sees part of the value. |

## What the provider still learns

Masking hides a value, not everything about it:

- **That it exists, and what kind it is.** `<PRIVYX_EMAIL_4FF5EF825E496E1A>` says that an
  email address was there.
- **When two values are equal.** The same value gets the same token in every
  session, from the default [anchor](../guide/masking.md#anchors) or the
  `hash` or `encrypt` operator; with `anchor.secret: ""`, within a session.
- **Everything that was not detected**, and the shape of the conversation
  around it.

A short unkeyed `hash` token can be matched by anyone who guesses the value.
Prefer `pseudonym` with an anchor secret when that matters.

## Security

| Limit | What to do |
|---|---|
| The proxy has no authentication and no notion of users. Anyone who can reach its port can send requests through it, and can name any session in the `x-privyx-session` header. | Keep it on `127.0.0.1`, or put an authenticating reverse proxy in front; see [Deployment](../guide/deployment.md#who-can-reach-it). Set session ids in your backend. |
| The vault and `--map` files hold original values in plain text, unless you use the `encrypt` operator. | Protect them like the data they mirror, set a [`vault.ttl`](../guide/sessions.md#expiring-sessions), or use [`encrypt`](../guide/masking.md#encrypt). |
| The `llm` detector sends unmasked text to the model it asks. | Use a provider you already trust with that text, or a model you host. |
| Privyx does not defend against prompt injection. Tool-call arguments are restored before your tool runs, so if injected instructions make the model call a tool that sends data elsewhere, real values are sent. | Limit what your tools may do, as you would without Privyx. |
| Your side still has everything. The client's own transcripts, logs, and terminal hold original values. | Protect them as before; Privyx changes only what leaves for the provider. |
| A plugin runs inside the proxy and sees unmasked text. | Load only code you trust. |
| Privyx is a technical control, not a compliance certificate. Masking can support a privacy program; it does not replace a data processing agreement or a legal assessment. | Treat it as one measure among several. |

## Maturity

Privyx is in its `0.1.x` series. New masking behaviors ship as opt-in in patch
releases; default changes are batched in minor releases. See
[Upgrading](../guide/upgrading.md) for versioning and deprecation, and the
[changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md) for changes.

If you find a case where a detected value reaches the provider, or a token is
restored wrongly, please report it privately; see the
[security policy](https://github.com/ohp1x/privyx/security/policy).
