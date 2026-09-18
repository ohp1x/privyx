# Data Handling

## In-Transit

- Request/response bodies are processed in memory only.
- Plaintext PII never crosses the boundary to the provider.
- Pseudonyms are the only representation the provider sees.

## At-Rest

- Session mappings persist in the configured vault.
- Default `MemoryVault` keeps nothing across restarts.
- `SQLiteVault` / `RedisVault` persist mappings — secure the store.

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
  `session.restore`, `proxy.request`, `proxy.response`, and `proxy.error` — see
  [audit events](../observability/audit-events.md) for the full schema.
- It records entity **types and counts** and request metadata — **never** the
  matched text, original values, or pseudonyms. This is enforced at the API: the
  `transform` helper takes a `{type: count}` histogram (not spans), and
  `proxy.error` records an exception's **class name**, never its message (which
  could echo payload text).
- Disable it with `audit.enabled: false` / `PRIVYX_AUDIT_ENABLED=false`.

## Data Minimization

- Sessions only store mappings for values actually pseudonymized.
- `delete()` on the vault removes a session's mappings.
- Vault TTL (Redis) expires idle sessions.
