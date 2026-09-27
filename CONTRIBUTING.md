# Contributing to Privyx

Thanks for your interest! Here's how to get started.

## Setup

```bash
uv sync --all-extras
```

## Development Loop

```bash
make check   # lint + typecheck
make test    # unit + property tests
make coverage  # tests + coverage report (terminal + htmlcov/)
make vulns     # known vulnerabilities in the locked dependencies (pip-audit)
```

## Design Principles

Read `docs/architecture/overview.md`. The non-negotiables:

1. Core has zero dependency on FastAPI or provider SDKs.
2. Everything privacy-relevant is a protocol/plugin.
3. Session state lives in the vault, never just process memory.
4. Streaming algorithms must have property tests for chunk boundaries.

## Commit Messages

Use conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## Branching

- `main` is stable.
- Feature work on `feat/<name>` branches.
- PRs are squash-merged.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Report
unacceptable behavior to security@privyx.io.
