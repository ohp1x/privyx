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
make bench     # timing of the request path (machine-dependent, not run in CI)
make docs      # serve the documentation site locally
```

## Design Principles

Read `docs/architecture/overview.md`. The non-negotiables:

1. Core has zero dependency on FastAPI or provider SDKs.
2. Everything privacy-relevant is a protocol/plugin.
3. Session state lives in the vault, never just process memory.
4. Streaming algorithms must have property tests for chunk boundaries.

## Documentation

The site at [ohp1x.github.io/privyx](https://ohp1x.github.io/privyx/) is built
from `docs/` with MkDocs Material. `make docs` serves it locally, and the CI
build is strict: a broken link, a missing anchor, or a page left out of the
navigation fails the check.

- Run every command you document and paste its real output.
- Code a reader copies should be code that runs. The scripts in `examples/`
  and the plugins in `plugins/` are run by the test suite and embedded in the
  pages with `--8<--`, so the docs show them as they are.
- A test fails when a setting, environment variable, or CLI option is missing
  from `docs/guide/configuration.md` or `docs/guide/cli.md`.
- The images in `docs/assets/` are regenerated with the files in
  `scripts/docs-assets/`.
- Write a fake key in two parts wherever it has to appear, as the tests and
  the tapes do: the repository holds no key-shaped string.

## Commit Messages

Use conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## Branching

- `main` is stable.
- Feature work on `feat/<name>` branches.
- PRs are squash-merged.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Report
unacceptable behavior through a [private report](https://github.com/ohp1x/privyx/security/advisories/new) on GitHub.
