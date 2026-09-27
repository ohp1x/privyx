# Privyx

AI data privacy gateway: a drop-in proxy for LLM providers that pseudonymizes
sensitive data before a request leaves your machine and restores it in the
reply, batch or streaming, without changing your client.

## Quick start

```bash
docker run -d --name privyx -p 127.0.0.1:8000:8000 \
  -e PRIVYX_UPSTREAM_URL=https://api.openai.com \
  -e PRIVYX_CONFIG=/app/configs/default.yaml \
  -v privyx-data:/data \
  ohp1x/privyx
```

Privyx detects emails, phone numbers, credit cards, IP addresses, SSNs, and
secrets (API keys, bearer tokens, JWTs, private keys, passwords in URLs).
`/app/configs/default.yaml` also detects values assigned to secret-looking
names (`password=…`).

Point your client at the proxy instead of the provider:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1       # OpenAI-compatible clients
export ANTHROPIC_BASE_URL=http://localhost:8000       # with PRIVYX_UPSTREAM_URL=https://api.anthropic.com
```

The client keeps its own API key; Privyx relays it upstream. To hold the key
in Privyx instead, set `PRIVYX_API_KEY` (it then replaces the client's key on
every request).

Keep the port on `127.0.0.1` unless something in front of it authenticates
callers: the proxy has no authentication of its own, so anyone who can reach
it can send requests through it, with your key if `PRIVYX_API_KEY` is set.

## Configuration

Common settings come from environment variables:

| Variable | Purpose |
|---|---|
| `PRIVYX_UPSTREAM_URL` | Provider origin, e.g. `https://api.openai.com` |
| `PRIVYX_API_KEY` | Upstream key held by Privyx (optional) |
| `PRIVYX_OPENAI_API_KEY`, `PRIVYX_ANTHROPIC_API_KEY` | Per-provider keys, used when `provider.type` is set in a config file |
| `PRIVYX_SESSION_STRATEGY` | `ephemeral` (default), `client`, or `conversation` |
| `PRIVYX_VAULT` | Session vault: `memory` (default), `sqlite`, or `redis` |
| `PRIVYX_REDIS_URL` | Redis URL when `PRIVYX_VAULT=redis` |
| `PRIVYX_ENCRYPT_KEY` | Key for the `encrypt` operator (64 hex chars) |
| `PRIVYX_ANCHOR_SECRET` | HMAC secret that keeps pseudonyms stable across turns and restarts |
| `PRIVYX_LOG_LEVEL` | `debug`, `info` (default), `warning`, … |
| `PRIVYX_AUDIT_ENABLED`, `PRIVYX_AUDIT_PATH` | PII-safe audit trail (default `/data/privyx-audit.log`) |
| `PRIVYX_SSL_CERTFILE`, `PRIVYX_SSL_KEYFILE` | Serve HTTPS directly |

For everything else (detectors, custom patterns, routes, operators), mount a
YAML config and point `PRIVYX_CONFIG` at it:

```bash
docker run -d -p 127.0.0.1:8000:8000 \
  -v "$PWD/privyx.yaml:/etc/privyx.yaml:ro" \
  -e PRIVYX_CONFIG=/etc/privyx.yaml \
  ohp1x/privyx
```

The image also ships the example configs under `/app/configs`
(`PRIVYX_CONFIG=/app/configs/strict.yaml`, for instance). Check the effective
settings, with credentials masked, using `docker run --rm ohp1x/privyx config --show`.

## Modes

The default command is `proxy`, a transparent reverse proxy that forwards every
path to the upstream. For the narrow chat-only gateway, which posts to one
fixed endpoint:

```bash
docker run -d -p 127.0.0.1:8000:8000 \
  -e PRIVYX_UPSTREAM_URL=https://api.openai.com/v1/chat/completions \
  ohp1x/privyx proxy --gateway
```

## Data and runtime

- `/data` holds the audit log and, with `PRIVYX_VAULT=sqlite`, the session
  database (`/data/privyx.db`). Mount a volume there to keep them.
- The container runs as the unprivileged user `privyx` (uid 1000).
- `GET /health` answers `{"status": "ok"}`; the image's `HEALTHCHECK` uses it.
- `GET /metrics` serves request, error, and masked-entity counters in the
  Prometheus text format. Like `/health` it needs no key, so publish the port
  only where those counts may be seen.

## Tags

- `X.Y.Z`: an exact release, e.g. `0.1.2`
- `X.Y`: the newest patch release of that line
- `latest`: the newest stable release (never a pre-release)

## More

Full documentation, the Python package, and the changelog:
[pypi.org/project/privyx](https://pypi.org/project/privyx/)
