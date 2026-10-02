---
description: Run one Privyx for a team. Docker Compose with a Redis vault, a gateway token checked by nginx, TLS, a provider key held in one place, and an audit trail.
---

# A shared gateway for a team

On a laptop, Privyx protects one person's requests. As a shared gateway it
does the same for a team or a set of services: everyone points their client
at one address, the provider key lives in one place, and one audit trail
shows what was masked.

Sharing changes two things. Other machines can now reach the proxy, so
something has to decide who may use it. And sessions have to survive restarts
and be visible to more than one process, so they move to Redis.

```text
clients ── HTTPS, gateway token ──► nginx ──► Privyx ──► provider
                                                │
                                                └── Redis (sessions)
```

**You need:** a Linux host with Docker, a provider API key, and a TLS
certificate for the name your clients will use. **Time:** thirty minutes.

## 1. Start Privyx and Redis

=== "Docker Compose"

    The repository's `docker-compose.yml` runs Privyx next to a Redis vault:

    ```bash
    git clone https://github.com/ohp1x/privyx.git
    cd privyx
    cp .env.example .env
    ```

    Every line in `.env` is commented out, and the compose file has a default
    for each. Uncomment and set these three:

    ```bash
    PRIVYX_UPSTREAM_URL=https://api.openai.com
    PRIVYX_API_KEY=sk-...
    PRIVYX_ANCHOR_SECRET=...
    ```

    Generate the anchor secret once with `openssl rand -hex 32` and keep it:
    it makes the same value get the same token in every session.

    ```bash
    docker compose up -d
    curl http://127.0.0.1:8000/health
    ```

    The first start builds the image, which takes a few minutes. After that
    you have:

    - Privyx on `127.0.0.1:8000`, reachable from this host only;
    - sessions in Redis, one per conversation, forgotten after a week
      without a request;
    - the audit log in the `privyx_data` volume;
    - `./configs` and `./plugins` mounted read-only at `/app/configs` and
      `/app/plugins`.

=== "Without Docker"

    ```bash
    pip install 'privyx[redis]'
    ```

    ```bash
    export PRIVYX_UPSTREAM_URL=https://api.openai.com
    export PRIVYX_API_KEY=sk-...
    export PRIVYX_VAULT=redis
    export PRIVYX_REDIS_URL=redis://localhost:6379/0
    export PRIVYX_VAULT_TTL=604800                # forget a session after a week idle
    export PRIVYX_SESSION_STRATEGY=conversation
    export PRIVYX_ANCHOR_SECRET=...               # openssl rand -hex 32, generated once
    privyx proxy
    ```

    Run it under your process manager of choice. It stops cleanly on
    `SIGTERM`: requests in flight get five seconds to finish.

`PRIVYX_API_KEY` is the provider key. Privyx sends it in place of whatever
key a client presents, so team members never hold the real one.

## 2. Decide who may use it

Privyx has no authentication of its own. With the provider key inside it,
anyone who can reach its port can spend that key. So the proxy stays on
`127.0.0.1`, and nginx in front of it accepts only clients that present a
**gateway token**.

The token goes where a client would normally put its API key, so nothing but
the base URL and the key changes on the client side.

```nginx
# /etc/nginx/conf.d/privyx.conf

# Clients present a gateway token where they would put a provider API key.
map "$http_authorization:$http_x_api_key" $gateway_ok {
    default                  0;
    "Bearer team-token-1:"   1;   # OpenAI-style clients
    ":team-token-1"          1;   # Anthropic-style clients
}

server {
    listen 443 ssl;
    server_name llm.internal.example;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location / {
        if ($gateway_ok = 0) {
            return 401;
        }
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Streamed replies (SSE) must not be buffered.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

Replace `team-token-1` with a long random string (`openssl rand -hex 24`),
and add a pair of lines per token to give each person or service its own.
nginx terminates TLS here, so Privyx itself keeps speaking plain HTTP on the
loopback interface.

Then tell Privyx never to pass on what a client sent as its key:

```yaml
# configs/gateway.yaml
proxy:
  forward_client_auth: false   # the gateway token stops here
```

and point it at that file, in `.env` for Docker Compose
(`PRIVYX_CONFIG=/app/configs/gateway.yaml`) or with `-c configs/gateway.yaml`.
Apply it with `docker compose up -d`, which recreates the container when
`.env` changed. After editing only the YAML file, use
`docker compose restart privyx`.

A client now looks like this:

```python
from openai import OpenAI

client = OpenAI(base_url="https://llm.internal.example/v1", api_key="team-token-1")
```

```bash
export ANTHROPIC_BASE_URL=https://llm.internal.example
export ANTHROPIC_API_KEY=team-token-1
```

Without the token, nginx answers `401` and the request never reaches Privyx.
With it, the provider receives the masked request and Privyx's own key, not
the token.

!!! note "Other ways to restrict access"

    A private network or VPN, firewall rules, or client certificates at the
    reverse proxy work as well. What matters is that the port Privyx listens
    on is not reachable by anyone you would not hand the provider key to.

## 3. Tighten what leaves

Two more settings suit a shared gateway. Add them to `configs/gateway.yaml`:

```yaml
# configs/gateway.yaml
proxy:
  forward_client_auth: false   # the gateway token stops here
  passthrough_unknown: false   # refuse paths Privyx cannot mask

operator:
  type: encrypt                # Redis holds ciphertext, not original values
```

- **`passthrough_unknown: false`** closes the gap that unrouted paths leave.
  By default a path Privyx has no route for, such as `/v1/embeddings`, is
  forwarded as the client sent it. With this setting it is refused:

    ```json
    {"error": {"message": "privyx: /v1/embeddings is not in proxy.routes and proxy.passthrough_unknown is false"}}
    ```

    This also refuses harmless paths such as `GET /v1/models`, which some
    clients call at startup.

- **`operator.type: encrypt`** stores AES-256-GCM ciphertext in the vault, so
  a copy of the Redis data reveals no original values. It needs a key:

    ```bash
    PRIVYX_ENCRYPT_KEY=...       # openssl rand -hex 32, in .env
    ```

    Losing this key means the stored sessions can no longer be restored. The
    `encrypt` operator derives its tokens from the key, so
    `PRIVYX_ANCHOR_SECRET` is no longer used.

Add your organization's [names and terms](custom-terms.md) to the same file,
then check that it loads before you apply it:

```bash
docker compose run --rm privyx doctor -c /app/configs/gateway.yaml
```

## 4. See what it does

The audit trail and the session list are available from inside the
container, and neither prints original values:

```bash
docker compose exec privyx privyx audit stats --since 24h
docker compose exec privyx privyx audit tail
docker compose exec privyx privyx session list
```

```console
$ docker compose exec privyx privyx session list
SESSION               LAST ACTIVE          CREATED              MAPPINGS
ses_7173e424c7a476d9  2026-10-02T14:51:32  2026-10-02T14:51:32  2

1 session(s) (vault: redis)
```

For monitoring, Privyx serves two endpoints that need no key:

- `GET /health` answers `{"status":"ok"}`.
- `GET /metrics` serves request, error, and masked-entity counters in the
  Prometheus text format.

Scrape them on `127.0.0.1:8000` from the host. Through nginx they sit behind
the token like everything else.

## 5. Keep it running

- **Upgrade.** `git pull`, then `docker compose up -d --build`. Requests in
  flight get five seconds to finish; sessions stay in Redis.
- **Session lifetime.** The compose file expires a session after a week
  without a request (`PRIVYX_VAULT_TTL`). To delete older ones by hand:
  `privyx session prune --older-than 7d`, with `--dry-run` to look first.
- **Audit log rotation.** Privyx appends to one file and keeps it open.
  Rotate it with logrotate's `copytruncate`.
- **More than one instance.** Several Privyx containers behind a load
  balancer can share the same Redis: a conversation's session is found by
  whichever instance receives the next turn. Give them all the same
  `PRIVYX_ANCHOR_SECRET` or `PRIVYX_ENCRYPT_KEY`, so that tokens come from
  the value and not from a counter two instances could advance at once.
- **Secrets.** `.env` now holds the provider key and the anchor secret or
  encryption key. Keep it readable by its owner only, and out of version
  control.

## Checklist

- Privyx listens on `127.0.0.1` only; nothing but the reverse proxy reaches
  it.
- Clients present a gateway token over HTTPS; the provider key is in
  `PRIVYX_API_KEY`.
- `proxy.forward_client_auth` is `false`.
- Sessions expire (`PRIVYX_VAULT_TTL`), and the vault is not reachable from
  outside the host.
- `proxy.passthrough_unknown` is `false`, unless clients need unmasked paths.
- Your own names and terms are in the detector, tested with `privyx detect`.
- Someone looks at `privyx audit stats` now and then.

## Next steps

- [Deployment](../guide/deployment.md): TLS without a reverse proxy, timeouts,
  and connection limits.
- [Docker](../docker.md): the image, its variables, and its volumes.
- [Threat model](../security/threat-model.md) and
  [Limitations](../security/limitations.md): what this setup protects against,
  and what it does not.
