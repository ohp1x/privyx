---
description: Install Privyx, see what it masks, and send a first request through it, from a coding agent or from any OpenAI or Anthropic client.
---

# Quickstart

In about five minutes: install Privyx, see what it masks, and send a request
that reaches the provider with its secrets and personal data replaced, and
comes back with them restored.

## 1. Install

Privyx needs Python 3.12 or newer.

=== "pip"

    ```bash
    pip install privyx
    ```

=== "uv"

    ```bash
    uv tool install privyx
    ```

=== "No install"

    ```bash
    uvx privyx --version
    ```

    With [`uvx`](https://docs.astral.sh/uv/guides/tools/), write `uvx privyx`
    wherever this page says `privyx`.

[Installation](installation.md) covers extras, Docker, and upgrades.

## 2. See what gets masked

`privyx detect` runs the same detector the proxy uses, on a text you give it:

```console
$ privyx detect --transform "DB_PASSWORD=hunter2 deploy to 10.0.4.17, cc dana@acme.example"
    12:19    SECRET            'hunter2'
    30:39    IP_ADDRESS        '10.0.4.17'
    44:61    EMAIL             'dana@acme.example'

DB_PASSWORD=<PRIVYX_SECRET_1> deploy to <PRIVYX_IP_ADDRESS_2>, cc <PRIVYX_EMAIL_3>
```

The last line is what a provider would receive. Pipe a file in to try it on
something real:

```bash
privyx detect --transform --stdin < app.log
```

![A terminal: privyx detect finds an email address, an IP address, a card number, a JWT, and two API keys in a log file, and prints the log with a token in place of each.](../assets/detect.gif)

Nothing has been sent anywhere so far: `detect` only reads and prints.

## 3. Send requests through Privyx

Pick the tab that matches what you use.

=== "A coding agent"

    `privyx run` starts a proxy on a free local port, launches the tool
    pointed at it, and stops the proxy when the tool exits:

    ```bash
    privyx run claude            # Claude Code
    privyx run codex             # Codex
    privyx run aider             # aider
    ```

    ```console
    $ privyx run claude
    Privyx proxy → https://api.anthropic.com
    Running: claude --settings '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:38383"}}'
      ANTHROPIC_BASE_URL = http://127.0.0.1:38383
    ```

    The tool starts as usual and keeps its own login or API key. Arguments
    after `--` go to the tool: `privyx run claude -- --continue`.

    When you leave the tool, see what was masked on the way:

    ```bash
    privyx audit stats
    ```

    The tutorial [Coding agents](../tutorials/coding-agents.md) goes on from
    here: your own names and project terms, other tools, and what to check.

=== "An application or another client"

    Start the proxy in front of the provider:

    ```bash
    privyx proxy --upstream https://api.openai.com
    ```

    Then point the client at Privyx instead of the provider. Only the base
    URL changes; the client keeps its own API key, which Privyx relays:

    ```bash
    export OPENAI_BASE_URL=http://localhost:8000/v1
    ```

    A quick check with `curl`:

    ```bash
    curl http://localhost:8000/v1/chat/completions \
      -H "Authorization: Bearer $OPENAI_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model": "gpt-4o-mini",
           "messages": [{"role": "user", "content": "Write a short greeting to alice@example.com"}]}'
    ```

    The provider sees `<PRIVYX_EMAIL_1>`; the reply you get has
    `alice@example.com` in it again.

    For an Anthropic client, start the proxy with
    `--upstream https://api.anthropic.com` and set
    `ANTHROPIC_BASE_URL=http://localhost:8000` (no `/v1`). One proxy forwards
    to one provider, so serving both at once takes two proxies on different
    ports (`--port`).

    The tutorial [An app on the OpenAI or Anthropic SDK](../tutorials/sdk-app.md)
    goes on from here: SDK code, streaming, tool calls, and one session per user.

!!! warning "The proxy has no authentication of its own"

    Anyone who can reach its port can send requests through it. Keep it on
    `127.0.0.1`, the default, unless something in front of it authenticates
    callers. See [Deployment](deployment.md).

## 4. Check what happened

Privyx records every exchange in an audit trail: event names, entity types,
and counts, never the content.

```console
$ privyx audit tail --no-follow
2026-10-02T14:32:56  session.created    req_d2360fcf7d4c  ses_46e1351fb7944510  source=ephemeral
2026-10-02T14:32:56  session.transform  req_d2360fcf7d4c  ses_46e1351fb7944510  entity_counts=EMAIL:1 transformations=1
2026-10-02T14:32:57  proxy.request      req_d2360fcf7d4c  ses_46e1351fb7944510  method=POST path=/v1/chat/completions schema=openai status=200 stream=false duration_ms=909.33 upstream=api.openai.com transform_ms=0.25
2026-10-02T14:32:57  session.restore    req_d2360fcf7d4c  ses_46e1351fb7944510  transformations=1
2026-10-02T14:32:57  proxy.response     req_d2360fcf7d4c  ses_46e1351fb7944510  status=200 stream=false restored=1 duration_ms=910.53 bytes=314
2026-10-02T14:32:57  session.deleted    req_d2360fcf7d4c  ses_46e1351fb7944510  reason=ephemeral_request_complete mapping_count=1
```

`session.transform` says one `EMAIL` was masked in the request, and
`session.restore` that one token was put back in the reply. On
`proxy.request`, `transform_ms` is the time masking took; the rest of
`duration_ms` is mostly the provider.

The trail is in `~/.privyx/audit.log`, unless `audit.path` names another
file; then name it too: `privyx audit tail FILE`.

## What is masked by default

With no config file, Privyx detects email addresses, phone numbers, credit
card numbers, IP addresses, US social security numbers, and secrets: vendor
API keys, bearer tokens, JWTs, private keys, passwords in URLs, and values
assigned to secret-looking names (`password=…`, `api_key: …`).

Names, organizations, and project terms need a word list or an NLP or LLM
detector. [Your own names and terms](../tutorials/custom-terms.md) shows how,
and [Detection](detection.md) has every option.

## Check your setup

```console
$ privyx config
Privyx 0.1.14
  Host: 127.0.0.1:8000
  Upstream: http://localhost:20128
  Provider: generic
  Vault: memory
  Session: ephemeral
  Detector: regex
  Policy: default
  Operator: pseudonym
```

`Upstream` is where requests go when nothing else is set, a local default;
`--upstream`, `PRIVYX_UPSTREAM_URL`, or a config file changes it.
`privyx config --show` prints every setting with credentials masked, and
`privyx doctor` exercises the configured detector, vault, and proxy end to end.

## Next steps

- [How it works](how-it-works.md): what happens to a request, and the terms
  these pages use.
- [Tutorials](../tutorials/index.md): a complete walkthrough per use case.
- [Configuration](configuration.md): the config file, environment variables,
  and every setting.
- [Limitations](../security/limitations.md): what Privyx does not mask.
