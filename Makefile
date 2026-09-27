.PHONY: install dev test coverage e2e lint format check vulns docs run proxy clean

install:
	uv sync --all-extras

dev:
	uv sync --extra dev

test:
	uv run pytest

coverage:
	uv run pytest --cov=privyx --cov-report=term --cov-report=html

e2e:
	uv run python scripts/e2e_claude.py
	uv run python scripts/e2e_claude.py --transparent

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

check:
	uv run ruff check src tests
	uv run mypy src

# Known vulnerabilities in every locked dependency (all extras, dev included).
vulns:
	uv export --frozen --all-extras --no-emit-project -q | uvx pip-audit --disable-pip -r /dev/stdin

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
