# Cryptography

## Anchors

With an anchor secret, the id of a `pseudonym` token is the first 16 hex
characters (64 bits) of `HMAC-SHA256(secret, value)`:

```text
ann@example.com  →  <PRIVYX_EMAIL_E55D8A16FDB2F399>
```

- Deterministic: the same value gets the same token in every session.
- Keyed: only holders of the secret can derive a token or test a guess
  against one.
- One-way: an HMAC cannot be reversed. Restoring a reply still needs the
  session's mapping in the vault.

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
