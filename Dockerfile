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

# Install default spaCy model for the presidio extra.  GitHub release downloads
# fail now and then with a 5xx, so try a few times with a growing pause.
RUN --mount=type=cache,target=/root/.cache/uv \
    url="https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"; \
    for pause in 15 30 60 120; do \
        uv pip install "$url" && exit 0; \
        echo "model download failed, retrying in ${pause}s" >&2; \
        sleep "$pause"; \
    done; \
    uv pip install "$url"


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd -r -g 1000 privyx && \
    useradd -r -u 1000 -g privyx -m -d /home/privyx -s /bin/bash privyx

# The anchor key goes on the /data volume, so tokens outlive the container.
ENV PATH="/app/bin:/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRIVYX_ANCHOR_KEY_PATH=/data/anchor.key

WORKDIR /app

COPY --from=builder --chown=privyx:privyx /app/.venv /app/.venv

# The image's defaults, each only when neither name of its setting is given,
# in any case: as ENV, they would conflict with the other name.  A compose file
# adds its own as DEFAULT_PRIVYX_*, and values it fixes as FIXED_PRIVYX_*.
# `privyx` is first on PATH, so that `docker exec` reads the same vault and
# audit trail as the proxy.
COPY --chmod=755 <<'EOF' /app/bin/privyx-env
# As the loader reads a name: `PRIVYX_` exactly, then the path in any case.
given() { env -0 | sed -zn 's/^PRIVYX_\([^=]*\)=..*/\1/p' | grep -qzix "$1"; }
fixed() {
    [ -n "$3" ] || return 0
    for name in $(env -0 | sed -zn 's/^PRIVYX_\([^=]*\)=.*/\1/p' | grep -zix "$2" | tr '\0' ' '); do
        unset "PRIVYX_$name"
    done
    export "PRIVYX_$1=$3"
}
default() {
    eval "current=\${PRIVYX_$1:-}"
    [ -n "$current$3" ] || return 0
    given "$2" || export "PRIVYX_$1=${current:-$3}"
}
fixed HOST SERVER__HOST "${FIXED_PRIVYX_HOST:-}"
fixed PORT SERVER__PORT "${FIXED_PRIVYX_PORT:-}"
default HOST SERVER__HOST 0.0.0.0
default PORT SERVER__PORT 8000
default AUDIT_PATH FEATURES__AUDIT__PATH /data/privyx-audit.log
default VAULT_DSN VAULT__DSN sqlite+aiosqlite:////data/privyx.db
default VAULT VAULT__TYPE "${DEFAULT_PRIVYX_VAULT:-}"
default REDIS_URL VAULT__REDIS_URL "${DEFAULT_PRIVYX_REDIS_URL:-}"
default VAULT_TTL VAULT__TTL "${DEFAULT_PRIVYX_VAULT_TTL:-}"
default SESSION_STRATEGY SESSION__STRATEGY "${DEFAULT_PRIVYX_SESSION_STRATEGY:-}"
default LOG_LEVEL LOG__LEVEL "${DEFAULT_PRIVYX_LOG_LEVEL:-}"
default AUDIT_ENABLED FEATURES__AUDIT__ENABLED "${DEFAULT_PRIVYX_AUDIT_ENABLED:-}"
EOF

COPY --chmod=755 <<'EOF' /app/bin/privyx
#!/bin/sh
. /app/bin/privyx-env
exec /app/.venv/bin/privyx "$@"
EOF

# Reads the port from the environment, where the defaults above always put it,
# by the type of the setting: no config file, plugin, or secret is loaded.
# TLS can come from a mounted file or the command, so a port that does not
# answer HTTP is tried over HTTPS, unverified: its certificate names no loopback.
COPY --chmod=755 <<'EOF' /app/bin/privyx-health
#!/bin/sh
. /app/bin/privyx-env
exec python -c '
import http.client, os, ssl, urllib.request
from pydantic import TypeAdapter
def is_port(name):
    return name == "PRIVYX_PORT" or (
        name.startswith("PRIVYX_") and name[7:].upper() == "SERVER__PORT"
    )
raw = next(v for k, v in os.environ.items() if v and is_port(k))
port = TypeAdapter(int).validate_python(raw)
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
except (OSError, http.client.HTTPException):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=context)
'
EOF

COPY --chown=privyx:privyx configs/ /app/configs/
COPY --chown=privyx:privyx plugins/ /app/plugins/

RUN mkdir -p /data /home/privyx/.privyx && \
    chown -R privyx:privyx /data /home/privyx/.privyx

USER privyx

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["/app/bin/privyx-health"]

ENTRYPOINT ["privyx"]
CMD ["proxy"]
