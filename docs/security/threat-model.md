# Threat Model

## Assets

- **Session mappings** — pseudonym → original. If leaked, deanonymization.
- **Anchor secret** — HMAC key. If leaked, pseudonym derivation is forgeable.
- **Request/response payloads** — may contain PII in transit.

## Trust Boundaries

```text
Client ──► [Privyx] ──► Provider
             ▲
             └── Vault (sessions)
```

- The client trusts Privyx to deanonymize its own responses.
- The provider must NEVER see plaintext PII (that is the point).
- The vault must be protected like a credential store.

## Threats & Mitigations

| Threat | Mitigation |
|---|---|
| Provider sees PII | Pseudonymize before forwarding |
| Vault leak | `operator.type: encrypt` stores AES-256-GCM ciphertext instead of plaintext (see [cryptography.md](cryptography.md)); the default `pseudonym`/`hash`/`faker` operators still store plaintext and rely on the vault being trusted |
| Log leakage | Default logging never prints payload text |
| Pseudonym collision | Counter + entity type + session isolation |
| Streaming boundary corruption | Hold-back scan + property tests |
| Session injection | Random `session_id` (uuid), vault-backed |
| Key exposure | `PRIVYX_ANCHOR_SECRET` / `PRIVYX_ENCRYPT_KEY` via env, never logged |
| Transport eavesdropping | Native TLS termination (`tls.enabled` / `PRIVYX_SSL_*` / `PRIVYX_TLS_*`) or a TLS-terminating reverse proxy in front — see [proxy.md](../architecture/proxy.md) |

## Assumptions

- The proxy either terminates TLS itself or sits behind a TLS-terminating reverse proxy; plaintext HTTP between the operator's network boundary and the client is not assumed safe.
- The vault is a trusted component **unless** `operator.type: encrypt` is selected, in which case a vault leak exposes ciphertext, not plaintext.
- HMAC anchoring requires the secret to remain secret.

## Out of Scope (v0.1)

- Multi-tenant authN/authZ (future work).
- Key rotation for active sessions.
- Encryption at rest for the *default* operators (`pseudonym`, `hash`, `faker`) — use `operator.type: encrypt` where this matters; see [cryptography.md](cryptography.md).
