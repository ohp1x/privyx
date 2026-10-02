---
description: What Privyx protects, whom it trusts, the threats it addresses and how, and what it leaves out of scope.
---

# Threat model

Privyx exists to keep sensitive values in requests to an AI provider from
reaching that provider, while the client still gets a usable reply. This page
states what it protects, whom it trusts, and where its protection ends.
[Limitations](limitations.md) lists the practical consequences.

## Assets

- **Original values**: the sensitive text in requests and replies.
- **Session mappings**: token → original value, in the vault and in
  `privyx mask --map` files. Whoever reads a mapping can undo the masking.
- **Anchor secret**: the HMAC key behind anchored tokens. Whoever holds it can
  test a guessed value against a token.
- **Encryption key**: the key of the `encrypt` operator. Whoever holds it and
  the vault can read the stored values.
- **Provider keys**: relayed from clients, or held by Privyx.

## Trust boundaries

```text
Client ──► [Privyx] ──► Provider
              │
              ├── Vault (sessions)
              └── Detector model (llm detector only)
```

- **The client is trusted with its own data.** It sends original values and
  gets original values back. Privyx does not authenticate clients, so anyone
  who can reach it is, to Privyx, that client.
- **The provider is not trusted with original values.** It should receive
  tokens in their place. That is the point.
- **The vault is trusted**, and must be protected like a credential store,
  unless the `encrypt` operator is used.
- **The model behind the `llm` detector sees unmasked text.** Configuring it
  means trusting it with that text.
- **Plugins are trusted code.** They run inside the proxy.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| The provider, or anyone with access to its logs or stored conversations, reads sensitive values | Detected values are replaced before a request is forwarded. A request that cannot be masked is not forwarded. Inside the masked parts of a request, a field Privyx does not know is masked, not passed through. |
| The provider infers a value from its token | A counter token (`<PRIVYX_EMAIL_1>`) carries nothing of the value. An anchored token carries 64 bits of a keyed HMAC, which cannot be tested without the secret. A `hash` token is unkeyed and can be matched by guessing; see [Masking](../guide/masking.md#hash). |
| Someone obtains a copy of the vault or of a map file | The SQLite file and map files are created readable by their owner only. `vault.ttl` limits how long a session exists. `operator.type: encrypt` stores AES-256-GCM ciphertext instead of plaintext (see [Cryptography](cryptography.md)); the other operators store plaintext and rely on the vault being protected. |
| Logs or the audit trail leak values | Logging never prints request or reply text at the default level. The audit trail records entity types and counts, and an error's class name, never its message. Error responses do not quote the request. A debug `log_file` can hold original values and is created readable by its owner only. |
| A reply tricks Privyx into revealing data | Restoring replaces only tokens that the request's own session issued. A token-shaped string from anywhere else is left as written. |
| Someone uses the proxy without permission, or names another caller's session | Privyx binds to `127.0.0.1` by default. Session ids it derives (`client`, `conversation`) are keyed on the caller's credential, and per-request ids are random. An explicit `x-privyx-session` header is trusted as sent. Beyond that, access control is the deployment's job; see [Deployment](../guide/deployment.md#who-can-reach-it). |
| Traffic between a client and Privyx is read | Native TLS termination (`tls.certfile` and `tls.keyfile`, or `PRIVYX_SSL_*` / `PRIVYX_TLS_*`) or a TLS-terminating reverse proxy in front; see [Deployment](../guide/deployment.md#https). |
| A token split across stream chunks reaches the client unrestored | The stream is restored with a hold-back scan, and property tests check that streamed output equals batch output for any chunking; see [Streaming](../architecture/streaming.md). |
| Two values get the same token | Counters are issued under a per-session lock within one process. Across instances, an anchor derives the token from the value. |
| Keys leak through process lists, shell history, or diagnostics | Keys come from the environment or a config file, never from a command-line flag. `privyx config --show` and the startup lines mask them. |
| A plugin does harm | Plugins are opt-in and load from local paths you list, never from installed packages. A plugin cannot replace a built-in component. |

## Assumptions

- The host that runs Privyx is trusted. Original values pass through the
  process's memory.
- The proxy either terminates TLS itself, sits behind a TLS-terminating
  reverse proxy, or is reached over the loopback interface. Plain HTTP across
  a network is not assumed safe.
- The vault is a trusted component, unless `operator.type: encrypt` is
  selected, in which case a vault leak exposes ciphertext, not plaintext.
- The anchor secret and the encryption key stay secret.
- The detector is configured for the data it sees. A value no detector finds
  is not protected.

## Out of scope

- **Authentication, authorization, and multi-tenancy.** Privyx trusts whoever
  can reach it.
- **Prompt injection.** Tool-call arguments are restored before your tool
  runs, so a tool that sends data elsewhere sends real values.
- **The client's side.** Transcripts, terminals, and logs of the client hold
  original values.
- **Side channels.** The provider sees that a value existed, its entity type,
  when two values are equal, and the length and timing of the conversation.
- **Key rotation for active sessions.**
- **Encryption at rest for the default operators** (`pseudonym`, `hash`,
  `faker`). Use `operator.type: encrypt` where this matters.

Report a suspected vulnerability privately; see the
[security policy](https://github.com/ohp1x/privyx/security/policy).
