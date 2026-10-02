---
description: What to decide before other machines use your Privyx. Where it listens, who may reach it, HTTPS, secrets, the vault, shutdown, logs, and running more than one instance.
---

# Deployment

On your own machine, the defaults are the deployment: Privyx listens on
`127.0.0.1`, keeps sessions in memory, and writes an audit log next to where
it runs. This page covers what to decide once other machines, or other
people, use the same proxy. For a complete worked setup, see
[A shared gateway for a team](../tutorials/team-gateway.md).

## Who can reach it

Privyx has **no authentication of its own**. Whoever can open a connection to
its port can send requests through it, with their own provider key, or with
yours if Privyx holds one (`PRIVYX_API_KEY`).

| Setup | How access is limited |
|---|---|
| Your own machine | The default bind address, `127.0.0.1`. Nothing to do. |
| A container | The proxy listens on `0.0.0.0` inside the container; publish the port on the host's loopback only: `-p 127.0.0.1:8000:8000`. |
| A shared host | Keep Privyx on `127.0.0.1` and put a reverse proxy in front that authenticates callers; see [the gateway token recipe](../tutorials/team-gateway.md#2-decide-who-may-use-it). |
| A private network | A VPN, firewall rules, or security groups that admit only the clients you trust with the provider key. |

`--host 0.0.0.0` (or `host:` in a config file) makes the proxy listen on
every interface. Do that only when one of the last three rows applies.

`GET /health` and `GET /metrics` need no key either. `/metrics` exposes
counts, never content, but those counts say how much traffic you have.

## HTTPS

A client that talks to Privyx over a network sends its prompt, unmasked, to
Privyx. That hop needs TLS as much as the one to the provider.

### Built into Privyx

Give the proxy a certificate and its key:

```bash
privyx proxy --upstream https://api.openai.com \
  --ssl-certfile /etc/privyx/fullchain.pem --ssl-keyfile /etc/privyx/privkey.pem
```

```console
$ curl https://llm.internal.example:8000/health
{"status":"ok"}
```

The same settings exist as `tls:` in a config file and as environment
variables:

```yaml
tls:
  certfile: /etc/privyx/fullchain.pem
  keyfile: /etc/privyx/privkey.pem
  keyfile_password: ""     # only for an encrypted key
  ca_certs: ""             # optional CA bundle
```

```bash
export PRIVYX_SSL_CERTFILE=/etc/privyx/fullchain.pem
export PRIVYX_SSL_KEYFILE=/etc/privyx/privkey.pem
```

HTTPS is on when both the certificate and the key are set; with only one of
them Privyx refuses to start. The proxy then serves HTTPS only, in both
modes.

### Behind a reverse proxy

A reverse proxy in front of Privyx can terminate TLS, renew certificates, and
authenticate callers. Privyx then keeps listening on `127.0.0.1` over plain
HTTP.

=== "nginx"

    ```nginx
    server {
        listen 443 ssl;
        server_name llm.internal.example;

        ssl_certificate     /etc/nginx/certs/fullchain.pem;
        ssl_certificate_key /etc/nginx/certs/privkey.pem;

        location / {
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

    Without `proxy_buffering off`, nginx holds a streamed reply back until it
    is complete, and the client receives it all at once. `proxy_read_timeout`
    should be at least Privyx's own `proxy.timeout`, 300 seconds by default.

=== "Caddy"

    ```caddy
    llm.internal.example {
        reverse_proxy 127.0.0.1:8000
    }
    ```

    For a public host name, Caddy obtains and renews the certificate by
    itself.

## Keys and secrets

| Secret | What it is | Where it goes |
|---|---|---|
| Provider key | The key Privyx sends upstream, if it holds one | `PRIVYX_API_KEY`, or `PRIVYX_OPENAI_API_KEY` / `PRIVYX_ANTHROPIC_API_KEY` |
| Anchor secret | Makes a value's token the same in every session | `PRIVYX_ANCHOR_SECRET` |
| Encryption key | Key of the `encrypt` operator | `PRIVYX_ENCRYPT_KEY` |

- Set them through the environment. There is no command-line flag for a key,
  since a command line ends up in shell history and in `ps`, and a config
  file tends to get committed. Privyx does not expand `${VAR}` in a config
  file.
- Generate the anchor secret and the encryption key with
  `openssl rand -hex 32`, once, and store them like any other credential.
  A new anchor secret gives every value a new token. A lost encryption key
  makes the stored sessions unrecoverable.
- `privyx config --show` prints the effective settings with keys, secrets,
  header values, word lists, and patterns masked, so you can check a
  deployment without printing them.

## Sessions and the vault

The default vault lives in the proxy's memory. That is fine for one process
and for sessions that may vanish on a restart. Otherwise:

| Need | Vault |
|---|---|
| Sessions survive a restart, one host | `sqlite` |
| Several instances, or sessions that outlive a host | `redis` |

Three settings go with a persistent vault:

- **`vault.ttl`.** Sessions hold original values. Without a time-to-live they
  stay until someone prunes them. Set one whenever `session.strategy` is
  `client` or `conversation`, or clients send their own session ids.
- **Protection of the store.** The SQLite file and the Redis instance contain
  the values Privyx hides from the provider. Restrict them like the data they
  mirror, or use the [`encrypt` operator](masking.md#encrypt) so that the
  vault holds ciphertext.
- **A local disk for SQLite.** The database uses a write-ahead log, which does
  not work on a network file system such as NFS.

See [Sessions and vault](sessions.md).

## Running it as a service

Privyx is a single foreground process that logs to standard output and stops
cleanly on `SIGTERM`. A systemd unit to start from:

```ini
# /etc/systemd/system/privyx.service
[Unit]
Description=Privyx privacy proxy
After=network-online.target
Wants=network-online.target

[Service]
User=privyx
Group=privyx
# The provider key and other secrets, as PRIVYX_... lines; mode 0600.
EnvironmentFile=/etc/privyx/privyx.env
WorkingDirectory=/var/lib/privyx
StateDirectory=privyx
ExecStart=/opt/privyx/bin/privyx proxy -c /etc/privyx/privyx.yaml
Restart=on-failure
# Requests in flight get 5 s to finish after SIGTERM.
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

It assumes a virtual environment in `/opt/privyx`
(`python -m venv /opt/privyx && /opt/privyx/bin/pip install privyx`) and a
`privyx` system user. The audit log and a SQLite vault with a relative path
land in the working directory, `/var/lib/privyx`.

For containers, see [Docker](../docker.md).

## Shutdown and restarts

On `SIGTERM` or Ctrl-C, requests still running get five seconds to finish.
Longer ones, such as a long streamed reply, are then cut off: the client sees
the connection close. That fits inside the ten seconds `docker stop` waits
before it kills a container.

What a restart loses depends on the vault. With `memory`, every session.
With `sqlite` or `redis`, nothing; and with an anchor secret, tokens are the
same after the restart even for a new session.

## Logs, audit trail, and metrics

- **Application log.** Standard output, as text or as JSON lines
  (`logging: json`). It never contains request or reply text at the default
  level. `log_file` adds a file that also receives third-party logs; at
  `log_level: debug` it can hold original values and is created readable by
  its owner only.
- **Audit trail.** One JSON object per line in `audit.path`, with counts and
  entity types only. Privyx keeps the file open, so rotate it with
  logrotate's `copytruncate`. See
  [Audit events and metrics](../observability/audit-events.md).
- **Metrics.** `GET /metrics` in the Prometheus text format: events, masked
  entities by type, errors by phase, restored tokens, and response time.
  The counters start at zero with each process.
- **Health.** `GET /health` answers `{"status":"ok"}` as long as the process
  serves requests. It does not call the provider or the vault.

## More than one instance

Privyx keeps no state of its own between requests besides the vault, so
several instances can run behind a load balancer when they share a Redis
vault. Any instance can serve any request of a session.

Give all instances the same anchor secret (or `encrypt` key). Tokens are then
derived from the value. Without one, tokens are numbered per session, and two
instances that mask the same session at the same moment could hand out the
same number. See [Scaling](../architecture/scaling.md).

One piece of state is per process: the text of Anthropic `thinking` blocks,
which Privyx remembers so that an echoed block still matches its signature.
After a restart, or on another instance, the block is masked again instead,
and the provider may reject the turn if the model wrote a sensitive value in
its thinking. Session affinity at the load balancer avoids that.

## A checklist

- The port is reachable only by clients you would hand the provider key to.
- Clients reach Privyx over HTTPS, or over the loopback interface.
- Secrets come from the environment; the files that hold them are readable
  by their owner only.
- A persistent vault has a `vault.ttl` and is protected like the data it
  mirrors.
- `proxy.passthrough_unknown: false`, unless clients need unmasked paths such
  as embeddings.
- Your own names and terms are in the detector; see
  [Your own names and terms](../tutorials/custom-terms.md).
- Someone reads `privyx audit stats`, or a dashboard on `/metrics`.

## See also

- [Proxy modes and routes](proxy.md): what is forwarded, and with which key.
- [Threat model](../security/threat-model.md) and
  [Limitations](../security/limitations.md).
- [Configuration reference](configuration.md).
