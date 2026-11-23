---
description: The cryptography behind anchored tokens and the encrypt operator, and how keys are supplied.
---

# Cryptography

## Anchors

With an anchor secret, the id of a `pseudonym` token is the first 16 hex
characters (64 bits) of `HMAC-SHA256(secret, value)`, or as many as
`operator.id_length` sets:

```text
ann@example.com  →  <PRIVYX_EMAIL_E55D8A16FDB2F399>
```

- Deterministic: the same value gets the same token in every session. If two
  values of one session share those characters, the later one gets a
  longer id from the same digest instead of taking the token.
- Keyed: only holders of the secret can derive a token or test a guess
  against one.
- One-way: an HMAC cannot be reversed. Restoring a reply still needs the
  session's mapping in the vault.

## Session storage

With `pseudonym`, `hash`, `faker`, `fpe`, and `partial`, the vault stores each
original value in plain text. That is acceptable only when the vault itself is
protected: Redis access control, owner-only permissions on the SQLite file.

The `encrypt` operator keeps plain text out of the vault. It stores the value
encrypted and decrypts it to restore a reply, so a leaked or shared vault
exposes no original value.

- **Cipher.** AES-256-GCM, from the `cryptography` package. The nonce is an
  HMAC of the value, so the same value always encrypts to the same bytes. That
  reveals only that two values are equal, which every reversible operator
  reveals through its tokens anyway, and it never reuses a nonce for two
  different values.
- **Token.** An ordinary token whose id is a keyed digest of the value:
  deterministic, but impossible to compute or test without the key. Its length
  follows [`operator.id_length`](../guide/masking.md#id-length).
- **Authenticated.** The GCM tag makes a corrupted or foreign value fail to
  decrypt. Privyx then leaves the token as it is instead of restoring garbage.
- **Streaming.** Batch and streamed replies are restored the same way, as with
  the other token operators.

`encrypt` needs the `crypto` extra (`pip install 'privyx[crypto]'`). Without the
package or without a key, Privyx stops at startup.

## Key handling

| Key | Setting | Environment variable |
|---|---|---|
| Anchor secret | `anchor.secret` | `PRIVYX_ANCHOR_SECRET` |
| Encryption key, 64 hex characters (32 bytes) | `operator.key` | `PRIVYX_ENCRYPT_KEY` |

- Generate either with `openssl rand -hex 32`, once, and keep it: a new anchor
  secret changes every anchored token, and a lost encryption key makes the
  stored sessions unrecoverable.
- `anchor.secret: auto`, the default, creates a per-user key in
  `~/.privyx/anchor.key`, readable by its owner only; see
  [Anchors](../guide/masking.md#anchors).
- In a config file, read a key from the environment with `${NAME}` or from a
  [password manager](../guide/configuration.md#secrets-from-a-password-manager)
  rather than writing it into the file.
- `privyx config show` prints both masked.
