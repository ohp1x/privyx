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
| Vault leak | Encrypt session payloads at rest (future: `encrypt.py`) |
| Log leakage | Default logging never prints payload text |
| Pseudonym collision | Counter + entity type + session isolation |
| Streaming boundary corruption | Trie/frontier algorithm + property tests |
| Session injection | Random `session_id` (uuid), vault-backed |
| Key exposure | `PRIVYX_ANCHOR_SECRET` via env, never logged |

## Assumptions

- The operator controls the network path; TLS is assumed in front.
- The vault is a trusted component.
- HMAC anchoring requires the secret to remain secret.

## Out of Scope (v0.1)

- Multi-tenant authN/authZ (future work).
- Key rotation for active sessions.
- AT-rest encryption of vault (placeholder `EncryptOperator`).
