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

- Privacy logic never logs payload text by default.
- `observability/audit.py` records events (session created, transform) with
  counts, not content.
- `security/redaction.py` can scrub additional fields if a caller opts in.

## Data Minimization

- Sessions only store mappings for values actually pseudonymized.
- `delete()` on the vault removes a session's mappings.
- Vault TTL (Redis) expires idle sessions.
