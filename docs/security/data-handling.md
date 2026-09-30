# Data Handling

## In-Transit

- Request/response bodies are processed in memory only.
- Plaintext PII never crosses the boundary to the provider.
- Pseudonyms are the only representation the provider sees.

## At-Rest

- Session mappings persist in the configured vault.
- Default `MemoryVault` keeps nothing across restarts.
- `SQLiteVault` / `RedisVault` persist mappings — secure the store.
- The `sqlite` vault file (with its `-wal` and `-shm` files) and
  `privyx mask --map` files are created readable by their owner only. An
  existing file keeps its mode: `chmod 600` it.

## Logging

- Privacy logic never logs payload text.
- Application logs (`observability/logging.py`) go to stdout as text or JSON
  (`logging: text|json`); the JSON formatter serializes with `json.dumps`, so a
  message can never corrupt the record.
- The audit trail (`observability/audit.py`) is separate: one JSON object per
  line appended to a dedicated file (`audit.path`, default `privyx-audit.log`).
  Every record shares a versioned envelope (`schema_version`, ISO-8601 `time`,
  epoch `ts`, `event`, `request_id`, `session_id`) so a downstream reader has a
  stable contract, and one `request_id` ties every event of one proxied exchange
  together. The vocabulary is `session.created`, `session.transform`,
  `session.restore`, `session.deleted`, `proxy.request`, `proxy.response`, and
  `proxy.error` — see [audit events](../observability/audit-events.md) for the
  full schema.
- It records entity **types and counts** and request metadata — **never** the
  matched text, original values, or pseudonyms. This is enforced at the API: the
  `transform` helper takes a `{type: count}` histogram (not spans), and
  `proxy.error` records an exception's **class name**, never its message (which
  could echo payload text).
- Disable it with `audit.enabled: false` / `PRIVYX_AUDIT_ENABLED=false`.

## Data Minimization

- Sessions only store mappings for values actually pseudonymized.
- Default `ephemeral` proxy sessions never reach the vault: each lives in the
  proxy's memory for its one request and is dropped when the response completes
  or the client disconnects, which `session.deleted` records.
- Explicit-header and derived `client` / `conversation` sessions are retained for
  continuity and are not deleted at the end of each request. Bound their
  lifetime with `vault.ttl` or `privyx session prune`.
- `vault.ttl` (seconds) expires sessions idle that long on every backend. Redis
  expires the key itself; the memory and SQLite vaults stop returning an expired
  session at once and delete it from the store the next time a session is
  created. TTL expiry writes no audit event.
- `privyx session prune --older-than 7d` deletes idle sessions on demand and
  audits each one as `session.deleted` with `reason: prune`.
- `delete()` on the vault removes a session's mappings.
