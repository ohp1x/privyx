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

By default (`pseudonym`, `hash`, `faker`) session mappings store the original
value in plaintext in the vault. This is acceptable when the vault is trusted
(Redis ACLs, SQLite file permissions).

The `encrypt` operator removes plaintext from the vault: it stores the
**ciphertext** and decrypts on restore, so a leaked or shared vault exposes no
original PII.

- **Cipher:** AES-256-GCM (`cryptography`), with a **deterministic**, SIV-style
  nonce derived from the value (`security/crypto.py`). The same value therefore
  produces the same token every time (dedup, idempotent writes); the only
  information revealed is value *equality*, which every reversible operator
  already reveals.
- **Token:** an ordinary codec token whose identifier is a keyed HMAC digest of
  the value — deterministic, but unguessable without the key.
- **Authenticated:** the GCM tag means a corrupted or foreign token fails to
  decrypt and is passed through untouched, never restored to garbage.
- **Streaming:** identical to the other token operators; the decrypting resolver
  is shared by the batch and streaming paths, so `stream == batch` holds.

`encrypt` needs the optional `crypto` extra (`pip install privyx[crypto]`);
selecting it without the package — or without a key — fails at startup.

## Key Handling

- Keys come from config / environment, never from code:
  - anchors: `PRIVYX_ANCHOR_SECRET` / `anchor.secret`
  - the encrypt operator: `PRIVYX_ENCRYPT_KEY` / `operator.key` (64 hex chars,
    a 32-byte AES-256 key)
- Generate: `openssl rand -hex 32`
- Never log keys.
