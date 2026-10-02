---
description: Every response Privyx generates itself, with its status, error type, cause, and where to look next.
---

# Errors

An error response from the provider reaches the client exactly as the provider
sent it. This page is about the responses Privyx generates itself, when it
cannot complete a request.

Privyx fails closed. If it cannot mask a request, it does not forward it, and
if it cannot restore a reply, it does not return it.

## Responses with an error type

The body is `{"error": {"type": …, "message": …}}`:

```json
{"error": {"type": "privyx_scan_failed", "message": "privyx could not scan this request for sensitive data, so it was not forwarded; see the privyx logs"}}
```

| Status | `error.type` | When | Forwarded? |
|---|---|---|---|
| `400` | `privyx_invalid_request` | The body of a request on a masked path is not a JSON object: it is compressed, form-encoded, or not JSON. Privyx cannot mask it. | No |
| `502` | `privyx_upstream_unreachable` | No response from the upstream: the connection was refused or dropped. A connection that could not be opened was retried twice first, 0.5 s and 1 s apart. | Possibly |
| `503` | `privyx_scan_failed` | Masking the request failed, for example because the [LLM detector](../guide/detection.md#llm-detector) timed out or a detector plugin raised. | No |
| `503` | `privyx_vault_unavailable` | The session vault failed: Redis is down or has not answered for 5 s, or a SQLite call failed. | No, or the reply was not returned |
| `503` | `privyx_upstream_busy` | All `proxy.max_connections` connections stayed busy for 10 s. Sent with `Retry-After: 10`. | No |
| `504` | `privyx_upstream_timeout` | The upstream took longer than `proxy.connect_timeout` to accept the connection, or longer than `proxy.timeout` to send the next bytes. | Possibly |

"Possibly" means the request may have reached the provider, and may have
been billed, without a response coming back.

The message of an upstream error ends with the class of the underlying
error, such as `(ConnectError)`. No message ever quotes the request.

A stream that fails after its first bytes cannot change its status any more:
Privyx closes the connection instead.

## Responses without an error type

| Status | Body | When |
|---|---|---|
| `403` | `{"error": {"message": "privyx: /v1/embeddings is not in proxy.routes and proxy.passthrough_unknown is false"}}` | Transparent mode, a path without a route, and [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route). |
| `404` | `{"detail":"Not Found"}` | Gateway mode, a path that is not served. |
| `426` | `{"error": {"message": "privyx: WebSocket is not supported; use HTTP"}}` | A WebSocket upgrade. Privyx cannot relay one, so it could not mask its frames. |

## Finding the cause

Each error with a type is also a `proxy.error` event in the
[audit trail](../observability/audit-events.md#proxyerror), with the phase
that failed and the class of the error:

```console
$ privyx audit tail --no-follow -n 3
2026-10-02T14:45:14  session.created    req_1679b6cdb689  ses_6d82287dc98b4e63  source=ephemeral
2026-10-02T14:45:16  proxy.error        req_1679b6cdb689  ses_6d82287dc98b4e63  phase=transform error_type=DetectorError duration_ms=1463.72
2026-10-02T14:45:16  session.deleted    req_1679b6cdb689  ses_6d82287dc98b4e63  reason=ephemeral_request_complete mapping_count=0
```

| `phase` | What failed | Status |
|---|---|---|
| `transform` | Masking the request | `503` `privyx_scan_failed` |
| `vault` | Reading or writing the session | `503` `privyx_vault_unavailable` |
| `upstream` | Reaching the provider | `502`, `503` `privyx_upstream_busy`, or `504` |
| `stream` | A streamed reply, after it started | The connection closes |
| `response` | Relaying a reply with nothing to restore | The connection closes |

The proxy's log has one `WARNING` line per error, with the class and never
the message:

```text
2026-10-02 14:45:16,336 [WARNING] privyx.proxy.transparent: request not forwarded: DetectorError while masking it
```

The full traceback is written only at `log_level: debug`. Set `log_file` to
keep it out of the console: at `debug` that file can hold original values, so
Privyx creates it readable by its owner only. Under `privyx run`, log lines
go to `log_file` only, since the tool owns the terminal.

## In an SDK

The official SDKs raise their usual exception for the status, and retry
`5xx` responses on their own before they do. With the OpenAI SDK the error
type is on the exception:

```python
import openai

try:
    reply = client.chat.completions.create(...)
except openai.APIStatusError as error:
    if (error.type or "").startswith("privyx_"):
        ...  # for example privyx_upstream_unreachable, with error.status_code 502
    raise
```

With the Anthropic SDK, read it from `error.body["error"]["type"]`.

## See also

- [Troubleshooting](../guide/troubleshooting.md): symptoms and what to do
  about them.
- [Audit events and metrics](../observability/audit-events.md):
  `privyx_proxy_errors_total` counts errors by phase.
