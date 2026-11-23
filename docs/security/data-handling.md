---
description: "Where original values exist while Privyx runs: in transit, at rest, in logs and the audit trail, and how long sessions are kept."
---

# Data handling

Where original values exist while Privyx runs, and for how long. The
[threat model](threat-model.md) says why each matters.

## In transit

- Request and reply bodies are processed in memory and never written to disk,
  unless you turn on the [session tap](../observability/tap.md).
- On a masking route, the values the detector finds and the policy selects are
  replaced before the request is forwarded. Values no detector finds, and
  fields Privyx leaves alone, reach the provider as sent; see
  [Limitations](limitations.md).
- The provider sees the replacements and the rest of the request. The
  `partial` operator leaves part of each value visible on purpose.

## At rest

- Session mappings, which hold the original values, are kept in the vault.
  The default `memory` vault keeps nothing across a restart; `sqlite` and
  `redis` keep the mappings until they expire or are pruned, so protect them
  like the data itself. The [`encrypt` operator](cryptography.md#session-storage)
  stores ciphertext instead.
- On POSIX, the SQLite vault file, with its `-wal` and `-shm` files, and the
  audit log are made readable by their owner only (`0600`) whenever Privyx
  opens them. A new `privyx mask --map` file is created owner-only; an existing
  one keeps its mode, so `chmod 600` it.
- The session tap (`proxy.tap`, off by default) writes requests and replies,
  with their original values, to `proxy.tap_dir`. The directory is created
  with mode `0700` and each file with `0600`.

## Logs and the audit trail

- Privyx's own log lines never contain request or reply text. They go to
  standard output as text or JSON lines (`log.format: text` or `json`).
- A `log.file` at `log.level: debug` also receives the logs of third-party
  libraries, which can include vault rows with original values. A new file is
  created with mode `0600`; on POSIX, an existing file with broader
  permissions or another owner is refused before anything is written.
- The audit trail is a separate file (`features.audit.path`, default
  `~/.privyx/audit.log`), one JSON object per line. It records event names,
  entity types, and counts, never matched text, original values, or tokens,
  and for an error only the exception's class name, since its message could
  quote the request. Events are `session.*` (created, transform, restore,
  deleted), `proxy.*` (request, passthrough, blocked, response, error), and
  `run.*` (unproxied, budget_exceeded); see
  [Audit events and metrics](../observability/audit-events.md).
- Turn the trail off with `features.audit: false` or
  `PRIVYX_AUDIT_ENABLED=false`.

## How long sessions are kept

- A session holds a mapping only for the values that were actually masked.
- The default `ephemeral` sessions never reach the vault. Each lives in the
  proxy's memory for one request and is dropped when the reply is complete or
  the client disconnects; `session.deleted` records it.
- Sessions chosen by a header, or by the `client` and `conversation`
  strategies, are kept so that the next request can use them. Limit their
  lifetime with `vault.ttl` or `privyx session prune`.
- `vault.ttl` expires a session after that many seconds without a request, on
  every backend. Redis expires the key itself; the `memory` and `sqlite`
  vaults stop returning an expired session at once and delete it the next
  time a session is created. Expiry writes no audit event.
- `privyx session prune --older-than 7d` deletes idle sessions on demand and
  records each as `session.deleted` with `reason: prune`.
