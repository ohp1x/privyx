---
description: Symptoms you may run into with Privyx, what causes each, and how to fix it.
---

# Troubleshooting

Find the symptom, then the cause. Three commands answer most questions:

```bash
privyx detect -c my.yaml --transform "a sample of your text"   # what would be masked
privyx audit tail FILE --no-follow                             # what happened to recent requests
privyx doctor -c my.yaml                                       # does this configuration work at all
```

## A value is not masked

**Check with `privyx detect`.** It uses the same detector and policy as the
proxy:

```bash
privyx detect -c my.yaml --transform "the text that went out"
```

| What you see | Cause | Fix |
|---|---|---|
| The value is not in the list | No detector knows it. Names, companies, and project terms have no built-in pattern. | Add a [word list or a pattern](../tutorials/custom-terms.md). |
| `detect` finds it, the proxy does not | The proxy runs with another configuration. No file is read unless you pass `-c` or set `PRIVYX_CONFIG`. | Start the proxy with the same `-c`; `privyx config --path` prints the file in use, or `defaults`. |
| It appears only with `--no-policy` | The `strict` policy filters it out. | Add the entity type to `policy.allowed`. |
| It is masked in chat messages but not elsewhere | The request went to a path without a route, such as embeddings, or the value sits in a field Privyx leaves alone, such as `user` or a URL. | See [Paths without a route](proxy.md#paths-without-a-route) and [Limitations](../security/limitations.md#coverage). |

## The tool does not go through Privyx

`privyx run` prints this when the tool exits without having sent it a request:

```text
Warning: no request from claude reached Privyx; if it called its provider, it did so directly, without masking.
```

- The tool's own configuration outranks what `privyx run` set: a
  `model_provider` of your own in Codex's config, or a `--settings` of your
  own after `--` for Claude Code.
- Your Claude Code settings point `ANTHROPIC_BASE_URL` at a router or another
  proxy. Give that URL to Privyx instead: `privyx run -u URL claude`.
- For a tool Privyx does not know, the variable named with `--env-var` is not
  the one the tool reads.

The warning also appears, harmlessly, when the tool did nothing that needed
its provider, such as `claude --version`.

## The provider answers 404

Usually the base URL has one `/v1` too many or too few.

| Client | Base URL | Why |
|---|---|---|
| OpenAI SDK and tools built on it | `http://localhost:8000/v1` | The SDK appends `/chat/completions`. |
| Anthropic SDK, Claude Code | `http://localhost:8000` | The SDK appends `/v1/messages` itself. |

An Anthropic client with a base URL ending in `/v1` calls `/v1/v1/messages`.
Privyx masks that request like `/v1/messages`, and the provider answers `404`.
Fix the base URL.

## `502 privyx_upstream_unreachable`

Privyx could not connect to the upstream.

- **No upstream was set.** Without `--upstream`, `PRIVYX_UPSTREAM_URL`, or a
  config file, the upstream is a local default, `http://localhost:20128`.
  The first lines the proxy prints show where requests go:

    ```text
    Privyx transparent proxy listening on http://127.0.0.1:8000
    Upstream origin: http://localhost:20128
    ```

- The host name is misspelled, or the machine has no route to it. In a
  container, `localhost` is the container itself.

## `503 privyx_scan_failed`

A detector failed, so the request was not forwarded. Almost always this is
the [`llm` detector](detection.md#llm-detector): its provider is unreachable,
its key is missing, the model name is not one your account can use, or the
scan took longer than `llm_timeout`.

The audit trail has the class of the error, and the proxy's log one line:

```text
2026-10-02 14:45:16,336 [WARNING] privyx.proxy.transparent: request not forwarded: DetectorError while masking it
```

For the full traceback, set `log_level: debug` and a `log_file`. That file
can then hold original values. To let requests through when the model is
unavailable, at the price of a weaker scan, set `llm_fallback_on_error: true`.

## `503 privyx_vault_unavailable`

The session vault failed: Redis is down or did not answer within five
seconds, or a SQLite call failed. Check `vault.redis_url` or `vault.dsn`, and
that the SQLite file sits on a local disk. `privyx doctor -c my.yaml` writes
and reads a test session and reports what it finds.

## `400 privyx_invalid_request`

The body of a chat request was not a JSON object, so Privyx could not mask it
and did not forward it. With `curl`, send JSON and say so:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}'
```

A compressed request body causes the same answer.

## The provider answers 401

- The client has no key and Privyx holds none. In transparent mode Privyx
  relays the client's key; set one in the client, or `PRIVYX_API_KEY` in
  Privyx.
- Privyx holds the wrong key. A key in Privyx replaces the client's on every
  request.
- A config file says `api_key: ${SOME_VARIABLE}`. Privyx does not expand
  variables in config files, so that literal text is sent as the key. Use the
  environment variables instead.
- In gateway mode the client's key is never relayed; Privyx needs its own.

## A token shows up in an answer

The client sees `<PRIVYX_EMAIL_1>` where a value should be.

| Cause | Fix |
|---|---|
| The model changed the token: translated it, put a space in it, or wrapped it in other characters. Privyx restores a token as written, without its outer delimiters, or as a long id alone, and nothing else. | Try another [token format](masking.md#token-format), and check with your model which one it reproduces reliably. |
| The token comes from another session. With the default `ephemeral` strategy, a token from an earlier request means nothing in the next one. That happens when the provider stores the conversation (`previous_response_id`), or when text with tokens was saved and sent again. | Use a [session](sessions.md#which-session-a-request-uses): the `x-privyx-session` header, or the `client` or `conversation` strategy. |
| The session expired (`vault.ttl`) or lived in a `memory` vault that restarted. | Use a persistent vault, a longer TTL, or an [anchor](masking.md#anchors). |
| The operator is `redact`. | Redaction cannot be restored; that is its purpose. |

## A streamed answer arrives all at once

Something between the client and Privyx buffers the stream, usually a reverse
proxy. For nginx, set `proxy_buffering off`; see
[Deployment](deployment.md#behind-a-reverse-proxy). Privyx itself holds back
only a fragment that could be the start of a token.

## Privyx does not start

| Message | Cause |
|---|---|
| `Error: unknown setting 'detector.term' (did you mean 'terms'?)` | A misspelled key in the config file. |
| `Error: config file not found: my.yaml` | The path given with `-c` or `PRIVYX_CONFIG` does not exist. |
| ``Error: aiosqlite is required. Install with `pip install privyx[sqlite]`.`` | A setting needs an [extra](installation.md#extras) that is not installed. |
| `Error: operator type 'encrypt' requires a key; …` | `PRIVYX_ENCRYPT_KEY` is not set. |
| `Error: cannot write audit.path privyx-audit.log: Permission denied` | The audit log's directory is not writable; set `audit.path` or `PRIVYX_AUDIT_PATH`. |
| `Error: Both --ssl-certfile and --ssl-keyfile are required for HTTPS.` | Only one of the two TLS files was given. |
| `[Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): [errno 98] address already in use` | Another process uses the port; pick one with `--port`. |

## `privyx session list` shows no sessions

- The vault is `memory`. It lives inside the proxy process, so the
  `privyx session` commands, which run as a separate process, cannot see it.
  Use `sqlite` or `redis`.
- The session strategy is `ephemeral`. Such sessions last for one request and
  never reach the vault.
- The command reads another configuration than the proxy. Pass the same `-c`
  and the same `PRIVYX_*` variables.

## `privyx audit` finds no file

```text
Error: [Errno 2] No such file or directory: 'privyx-audit.log'
```

The audit path is relative to where the proxy runs. Run `privyx audit` in that
directory, or name the file: `privyx audit stats /path/to/privyx-audit.log`.
For `privyx run` the file is `~/.local/state/privyx/audit.log`.

## Anthropic rejects a turn after a restart

With extended thinking, the provider signs each thinking block, and Privyx
remembers the signed text so that the block still matches when the client
sends it back. That memory lives in the proxy process. After a restart, or on
another instance, a conversation whose thinking contained a masked value can
be rejected by the provider. Start a new conversation, or keep a conversation
on one instance.

## Still stuck

Open a [discussion](https://github.com/ohp1x/privyx/discussions) or an
[issue](https://github.com/ohp1x/privyx/issues) with the output of
`privyx config --show` (credentials, word lists, and patterns are masked in
it) and the relevant lines of `privyx audit tail`. Report anything that looks
like a leak [privately](https://github.com/ohp1x/privyx/security/advisories/new).
