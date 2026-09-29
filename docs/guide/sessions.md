# Sessions and vault

A **session** holds the mapping from each token to the value it replaced.
Privyx uses it to restore the reply, so a request and its reply must share a
session. Sessions are kept in the **vault**.

## Which session a request uses

A request that carries an `x-privyx-session` header uses that session. Privyx
strips the header before forwarding, and the transparent proxy returns the id
it used in the response's `x-privyx-session` header.

Most clients never send the header, so `session.strategy` decides:

| Strategy | One session per | Use when |
|---|---|---|
| `ephemeral` (default) | Request | Requests are independent, or you are unsure. Nothing is shared between requests. |
| `client` | API key | One user per key, and conversations may share a mapping. |
| `conversation` | API key and first user message | A chat tool resends the conversation each turn. This is what `privyx run` uses. |

```yaml
session:
  strategy: conversation      # or PRIVYX_SESSION_STRATEGY=conversation
```

With `ephemeral`, a multi-turn conversation is masked from scratch every turn
and shows up as many sessions. `conversation` keeps one session per
conversation; two conversations that open with the same first message under the
same key share one. The trade-offs are covered in
[Proxy: Sessions](../architecture/proxy.md#sessions).

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
  ttl: 604800                 # a week
```

Or clean up by hand. `list` and `show` never print original values unless you
ask:

```console
$ privyx session list -c privyx.yaml
SESSION  LAST ACTIVE          CREATED              MAPPINGS
demo     2026-09-27T15:29:37  2026-09-27T15:29:37  1

1 session(s) (vault: sqlite)

$ privyx session show -c privyx.yaml demo
Session: demo
  Vault:      sqlite
  Created:    2026-09-27T15:29:37
  Updated:    2026-09-27T15:29:37
  Mappings:   1

  <PRIVYX_EMAIL_1>                 → al*************om

  (values masked; pass --reveal to show them)

$ privyx session prune -c privyx.yaml --older-than 7d --dry-run
```

`prune` records each deletion in the audit trail as `session.deleted` with
`reason: prune`. Expiry through `ttl` is not recorded.

## Masking outside the proxy

`privyx mask` and `privyx unmask` run the same engine on a string, a file, JSON,
or JSONL. The mapping goes either to a map file or to a vault session:

```bash
privyx mask --map map.json "mail alice@example.com"    # mail <PRIVYX_EMAIL_1>
privyx unmask --map map.json "hi <PRIVYX_EMAIL_1>"     # hi alice@example.com

privyx mask -c privyx.yaml --session demo -i request.json --path '$.messages'
```

A map file is self-contained and needs no vault. `--session` needs a
persistent vault, since a `memory` vault is gone when the command exits. See
the [CLI reference](cli.md#privyx-mask).
