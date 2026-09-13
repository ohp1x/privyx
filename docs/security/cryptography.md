# Cryptography

## Anchors

`HMACAnchor` derives pseudonyms with `HMAC-SHA256(secret, value)`:

```python
PRIVYX_<hexdigest>
```

- Deterministic: same value → same pseudonym (no vault needed).
- Keyed: only holders of the secret can derive/verify.
- One-way: HMAC is not reversible; deanonymization requires a lookup map.

## Session Storage

v0.1 stores session mappings in plaintext in the vault. This is acceptable
when the vault is trusted (Redis ACLs, SQLite file permissions), but
production deployments should enable at-rest encryption.

`EncryptOperator` is a placeholder for Fernet/AES-GCM-based tokens.

## Key Handling

- Keys come from `PRIVYX_ANCHOR_SECRET` / config, never from code.
- Generate: `openssl rand -hex 32`
- Never log keys. `security/redaction.py` scrubs known key patterns.
