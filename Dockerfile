# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

# Install uv from the official uv image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Compile bytecode for faster startup
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies in a separate layer to maximize cache reuse.  `dev` is
# an optional extra here, not a dependency group, so --no-dev alone would not
# keep pytest, ruff, and mypy out: --no-extra dev does.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --all-extras --no-extra dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --all-extras --no-extra dev

# Install default spaCy model for the presidio extra
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd -r -g 1000 privyx && \
    useradd -r -u 1000 -g privyx -m -d /home/privyx -s /bin/bash privyx

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRIVYX_HOST=0.0.0.0 \
    PRIVYX_PORT=8000 \
    PRIVYX_AUDIT_PATH=/data/privyx-audit.log \
    PRIVYX_VAULT_DSN=sqlite+aiosqlite:////data/privyx.db

WORKDIR /app

COPY --from=builder --chown=privyx:privyx /app/.venv /app/.venv

COPY --chown=privyx:privyx configs/ /app/configs/
COPY --chown=privyx:privyx plugins/ /app/plugins/

RUN mkdir -p /data /home/privyx/.privyx && \
    chown -R privyx:privyx /data /home/privyx/.privyx

USER privyx

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PRIVYX_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health')" || exit 1

ENTRYPOINT ["privyx"]
CMD ["proxy"]
