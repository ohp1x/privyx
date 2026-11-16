.PHONY: install dev test coverage e2e e2e-next-defaults e2e-labels bench bench-requests lint format check vulns docs run proxy clean

install:
	uv sync --all-extras

dev:
	uv sync --extra dev

test:
	uv run pytest

coverage:
	uv run pytest --cov=privyx --cov-report=term --cov-report=html

e2e:
	uv run python scripts/test-e2e/run.py $(E2E_ARGS)
	uv run python scripts/test-e2e/run.py --transparent $(E2E_ARGS)
	uv run python scripts/test-e2e/run.py --agent codex $(E2E_ARGS)
	uv run python scripts/test-e2e/run.py --agent codex --transparent $(E2E_ARGS)
	uv run python scripts/test-e2e/run.py --auto $(E2E_ARGS)
	uv run python scripts/test-e2e/run.py --transparent --auto $(E2E_ARGS)

e2e-next-defaults:
	$(MAKE) e2e E2E_ARGS=--next-defaults

e2e-labels:
	$(MAKE) e2e E2E_ARGS=--labels

# Timing of the request path; numbers vary by machine, so not run in CI.
bench:
	uv run python scripts/benchmark.py

# Focused cache-miss/hit baseline; pass e.g. BENCH_ARGS='--output /tmp/baseline.json'.
bench-requests:
	uv run python scripts/benchmark_requests.py $(BENCH_ARGS)

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

check:
	uv run ruff check src tests
	uv run mypy src

# Known vulnerabilities in every locked dependency (all extras, dev included).
vulns:
	uv export --color never --frozen --all-extras --no-emit-project -q | uvx pip-audit --disable-pip -r /dev/stdin

# Serve the documentation site at http://127.0.0.1:8000 with live reload.
docs:
	uv run --only-group docs mkdocs serve

run:
	uv run privyx

proxy:
	uv run privyx proxy

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov site build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
