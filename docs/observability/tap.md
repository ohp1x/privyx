---
description: Record each masked exchange before and after masking, and watch the diff live with privyx audit watch.
---

# Session tap

The [audit trail](audit-events.md) holds counts only. To see what masking did
to a request, turn on the tap: Privyx then writes each masked exchange, before
and after masking, to one file per session.

```yaml
# privyx.yaml
audit:
  tap: true                # off by default
  tap_dir: ~/.privyx/tap   # the default
```

!!! warning "The tap holds original values"
    Its files hold the requests as the client sent them and the replies as the
    client got them. The directory is created with mode `0700` and each file
    with `0600`. Keep the tap for debugging, and delete its files when you are
    done.

The tap works in transparent mode, the default and the mode `privyx run` uses.
With `tap: false` Privyx creates no tap directory and writes nothing.

## What a file holds

`<tap_dir>/<session id>.jsonl` gets two lines per exchange, sharing the
`time`, `ts`, `request_id`, and `session_id` of the audit events:

| `stage` | Fields |
|---|---|
| `request` | `path`, `schema`; `before` (the body the client sent) and `after` (the body sent upstream) |
| `response` | `status`, `stream`, `aborted` (streams); `upstream` (what the upstream answered) and `restored` (what the client got) |

A batch body is kept as JSON, a stream as its SSE text. A session id the
client chose that is not a plain name is hashed for the file name.

## Watching it live

```bash
privyx audit watch                 # every session
privyx audit watch SESSION_ID      # one session
```

It prints the last records, then each new one as it is written. Each place
that changed is one line, with removed text as `[-...-]` (red) and added text
as `{+...+}` (green):

```text
2026-11-09T14:02:11  request   req_8a7f9b0c1d2e  ses_01a116a0c5ca  /v1/messages
  mail [-alice@example.com-]{+<PRIVYX_EMAIL_1>+} about the invoice
2026-11-09T14:02:13  response  req_8a7f9b0c1d2e  ses_01a116a0c5ca  status 200 stream
  Sent to [-<PRIVYX_EMAIL_1>-]{+alice@example.com+}.
```

A streamed reply is shown as the text its events carry. `--no-follow` stops
after the last records, and `-n` sets how many come first.
