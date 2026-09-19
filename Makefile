.PHONY: install dev test e2e lint format check run proxy clean

install:
	uv sync --all-extras

dev:
	uv sync --extra dev

test:
	uv run pytest

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

run:
	uv run privyx

proxy:
	uv run privyx proxy

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
