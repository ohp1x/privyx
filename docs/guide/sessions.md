---
description: Which session a request uses, where sessions are stored, how long they live, and how to inspect and prune them.
---

# Sessions and vault

A **session** holds the mapping from each token to the value it replaced.
Privyx uses it to restore the reply, so a request and its reply must share a
session. Sessions are kept in the **vault**.

## Which session a request uses

A request that carries an `x-privyx-session` header uses that session. Privyx
strips the header before forwarding, and the transparent proxy returns the id
it used in the response's `x-privyx-session` header.

The header is for a backend you trust to pick session ids. Privyx does not
check who sends it, and whoever names a session can have its tokens restored.
Use an opaque, unguessable id, not a user name or an email address, and have
any reverse proxy in front of other clients
[remove the header](../tutorials/team-gateway.md#2-decide-who-may-use-it).

Most clients never send the header, so `session.strategy` decides:

| Strategy | One session per | Use when |
|---|---|---|
| `ephemeral` (default) | Request | Requests are independent, or you are unsure. Nothing is shared between requests. |
| `client` | Client credential | One user per credential, and conversations may share a mapping. |
| `conversation` | Client credential and first user message | A chat tool resends the conversation each turn. This is what `privyx run` uses. |

```yaml
session:
  strategy: conversation      # or PRIVYX_SESSION_STRATEGY=conversation
```

With `ephemeral`, a multi-turn conversation is masked from scratch every turn
and shows up as many sessions. `conversation` keeps one session per
conversation; two conversations that open with the same first message under the
same key share one. Gemini CLI opens each chat with the same message about
its environment (date, directory, files), so its chats in one directory on
one day share a session. The trade-offs are covered in
[Proxy: Sessions](../architecture/proxy.md#sessions).

These strategies read the credential the client sent to Privyx, in a header
or the query string, also with `forward_client_auth: false`, which only
decides what goes upstream. The fingerprint has limits:

- Callers that share a credential share a session under `client`, and under
  `conversation` when their first user message is the same. Each can then have
  the other's tokens restored, so give each caller its own credential.
- Without a credential, `client` falls back to `ephemeral`, while
  `conversation` uses the first user message alone: any two callers that open
  the same way share a session. A cookie or a client certificate that only
  your ingress checks is not a credential here: have the ingress send each
  caller its own API key or bearer token, or use `ephemeral`.

To keep tokens stable across sessions and restarts regardless of strategy, set
an [anchor secret](masking.md#anchors).

## Vault backends

| `vault.type` | Survives a restart | Shared between processes | Needs |
|---|---|---|---|
| `memory` (default) | No | No | Nothing |
| `sqlite` | Yes | Same host, through the file | `privyx[sqlite]` |
| `redis` | Yes | Yes | `privyx[redis]` and a Redis server |

```yaml
vault:
  type: sqlite
  dsn: /var/lib/privyx/privyx.db
```

```yaml
vault:
  type: redis
  redis_url: redis://localhost:6379/0
```

The `sqlite` vault writes through a write-ahead log, kept in `-wal` and `-shm`
files beside the database. Put the database on a local disk: the log does not
work on a network file system such as NFS.

The `memory` vault lives inside one process, so the `privyx session` commands,
which run as a separate process, cannot see it. Use `sqlite` or `redis` to
inspect or prune sessions.

The vault stores original values in plain text, except with the
[`encrypt` operator](masking.md#encrypt). Protect the SQLite file and the Redis
instance like any other store of personal data; see
[Data handling](../security/data-handling.md).

## Expiring sessions

An `ephemeral` session never enters the vault: it lives in the proxy's memory
for its one request and is dropped when the request finishes. Sticky sessions
(`client`, `conversation`, or a header) stay in the vault until something
removes them: in a `memory` vault until the process exits, in `sqlite` or
`redis` indefinitely. Set `vault.ttl` to expire a session after that many
seconds without a request:

```yaml
vault:
  type: sqlite
  ttl: 604800                 # a week, or PRIVYX_VAULT_TTL=604800
```

Set one whenever `session.strategy` is `client` or `conversation`: each
conversation leaves a session, with the original values it masked, and without
a TTL they pile up. `docker-compose.yml` sets a week.

Deleting or expiring a session applies to the exchanges that start afterwards.
An exchange already running keeps the mapping it was masked with, so its reply
is still restored. The next request with that id starts an empty session.

Or clean up by hand. `list` and `show` never print original values unless you
ask:

```console
$ privyx -c privyx.yaml session list
SESSION  LAST ACTIVE          CREATED              MAPPINGS
demo     2026-09-27T15:29:37  2026-09-27T15:29:37  1

1 session(s) (vault: sqlite)

$ privyx -c privyx.yaml session show demo
Session: demo
  Vault:      sqlite
  Created:    2026-09-27T15:29:37
  Updated:    2026-09-27T15:29:37
  Mappings:   1

  <PRIVYX_EMAIL_4FF5EF825E496E1A>  → al*************om

  (values masked; pass --reveal to show them)

$ privyx -c privyx.yaml session prune --older-than 7d --dry-run
```

`prune` records each deletion in the audit trail as `session.deleted` with
`reason: prune`. Expiry through `ttl` is not recorded.

## Masking outside the proxy

`privyx mask` and `privyx unmask` run the same engine on a string, a file, JSON,
or JSONL. The mapping goes either to a map file or to a vault session:

```bash
privyx mask --map map.json "mail alice@example.com"   # mail <PRIVYX_EMAIL_4FF5EF825E496E1A>
privyx unmask --map map.json "hi <PRIVYX_EMAIL_4FF5EF825E496E1A>"   # hi alice@example.com

privyx -c privyx.yaml mask --session demo -i request.json --path '$.messages'
```

A map file is self-contained and needs no vault. `--session` needs a
persistent vault, since a `memory` vault is gone when the command exits. See
the [CLI reference](cli.md#privyx-mask).

Unmask with the session or map file that masked the text. Without an
[anchor](masking.md#anchors), counter tokens are the same in every session, so
the wrong one can restore them as other values instead of failing.
