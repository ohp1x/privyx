# Security Policy

## Reporting a Vulnerability

Please report security issues privately. Do **not** open a public issue.

- Email: security@privyx.io
- Include: affected version, impact, and a minimal reproduction.

You will receive a response within 72 hours.

## Scope

In scope:

- Session mapping leakage
- Pseudonym forgery / anchor key exposure
- Plaintext PII reaching providers or logs
- Streaming deanonymization correctness

Out of scope (v0.1):

- Multi-tenant authN/authZ
- At-rest encryption of the vault (planned)

## Responsible Disclosure

We will acknowledge, fix, and credit reporters. Coordinated disclosure is
appreciated; we aim for a fix within 30 days for critical issues.
