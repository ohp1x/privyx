# Configuration

## Where settings come from

Privyx builds its settings from four layers. Each one overrides the one
before it:

1. Built-in defaults.
2. A YAML config file, passed with `-c` / `--config` or named by
   `PRIVYX_CONFIG`.
3. Environment variables (`PRIVYX_*`).
4. Command-line flags such as `--port` or `--upstream`.

No config file is read unless you name one. Without `-c` or `PRIVYX_CONFIG`,
Privyx runs on the built-in defaults plus the environment.

Sections merge key by key, so a file that sets only `vault.type` keeps the
defaults for the rest of `vault`. A list replaces the default rather than
extending it. Empty environment variables are ignored, so an unset variable
never clears a value from the file.

`privyx run` adds its own defaults (a `conversation` session strategy, an
anchor secret, and an audit trail in `~/.local/state/privyx/audit.log`) just
above the built-in ones, so your file and environment still override them.

A config with a misspelled setting, an unknown type, an invalid regex, or a
value of the wrong kind fails at startup, before any request is served. A
section whose `type` names a plugin is the exception to the first: keys Privyx
does not define there are the plugin's own options, and are passed to it.

```console
$ privyx proxy -c broken.yaml
Error: unknown setting 'detector.term' (did you mean 'terms'?)
$ privyx proxy -c broken.yaml
Error: unknown detector type: nope
$ privyx proxy -c broken.yaml
Error: invalid configuration: 1 validation error for Settings
vault.ttl
  Input should be greater than 0 [type=greater_than, input_value=0, input_type=int]
```

## Checking the result

```bash
privyx config                   # a summary
privyx config --show -c my.yaml # every setting as JSON, credentials masked
privyx doctor -c my.yaml        # build each component and run a request through it
```

## Example

```yaml
# privyx.yaml
provider:
  type: anthropic            # sends the key as x-api-key, adds anthropic-version

vault:
  type: sqlite               # needs privyx[sqlite]
  dsn: /var/lib/privyx/privyx.db
  ttl: 604800                # forget sessions idle for a week

session:
  strategy: conversation

detector:
  type: regex
  terms:
    PROJECT: [bluebird, nightjar]

audit:
  path: /var/log/privyx/audit.log
```

```bash
export PRIVYX_ANTHROPIC_API_KEY=...     # keep keys out of the file
privyx proxy -c privyx.yaml --upstream https://api.anthropic.com
```

The repository's
[`configs/`](https://github.com/ohp1x/privyx/tree/main/configs) folder has an
annotated `default.yaml`, a `strict.yaml`, and small examples. The Docker image
ships them under `/app/configs`.

## Reference

### Server and logging

| Key | Default | Description |
|---|---|---|
| `host` | `127.0.0.1` | Address the proxy binds to. |
| `port` | `8000` | Port the proxy listens on. |
| `log_level` | `info` | `debug`, `info`, `warning`, or `error`, for Privyx's own logs. |
| `logging` | `text` | Console format: `text` lines or `json` objects. |
| `log_file` | empty | File that also receives third-party logs at `log_level`. At `debug` it holds raw vault rows, which include original values; it is created with mode `0600`. |
| `upstream_url` | empty | Explicit upstream, the same as `--upstream`. Wins over `provider.base_url`. |

### `provider`

Where requests go and which key they carry.

| Key | Default | Description |
|---|---|---|
| `type` | `generic` | `generic`, `openai`, `anthropic`, or a plugin provider. Sets the default upstream and how the key is sent: `anthropic` sends it as `x-api-key` and adds `anthropic-version`. |
| `base_url` | empty | Upstream endpoint. Empty uses the default for `type`: `http://localhost:20128` for `generic`, `https://api.openai.com/v1/chat/completions` for `openai`, `https://api.anthropic.com/v1/messages` for `anthropic`. |
| `api_key` | empty | Key Privyx sends upstream in place of the client's. |
| `openai_api_key`, `anthropic_api_key`, `google_api_key` | empty | Per-type keys. The one matching `type` wins over `api_key`. |
| `headers` | `{}` | Extra headers sent with every upstream request. |

The upstream is the first non-empty of `upstream_url`, `provider.base_url`, and
the default for `provider.type`. The transparent proxy uses only its
`scheme://host[:port]` and appends the client's own path; the gateway posts to
it as written.

The key is the first non-empty of `provider.<type>_api_key` and
`provider.api_key`. With neither set, the transparent proxy relays the
client's own key and the gateway sends none. Keys have no command-line flag,
since a command line ends up in shell history and `ps`; set them through the
environment.

### `proxy`

| Key | Default | Description |
|---|---|---|
| `mode` | `transparent` | `transparent` forwards every path to the upstream origin; `gateway` serves Privyx's own endpoints and posts to one fixed URL. Same as `--transparent` / `--gateway`. |
| `routes` | ten chat paths | Path → wire schema (`openai`, `anthropic`, `responses`) for the requests Privyx masks. See [Routes](../architecture/proxy.md#routes). |
| `passthrough_unknown` | `true` | Forward paths not in `routes` unchanged. `false` answers them with `403` instead. |
| `forward_client_auth` | `true` | Relay the client's `Authorization` / `x-api-key`. `false` drops them so only the configured key is used. |
| `timeout` | `300` | Seconds the upstream may take to send the next bytes of a response, or to accept the next bytes of a request. A stream runs as long as data keeps coming. Both modes. |
| `connect_timeout` | `10` | Seconds to open a connection to the upstream. |
| `max_connections` | unset | Most connections open to the upstream at once. Unset means no limit. With a limit, a request that waits more than 10 s for a free connection gets a `503` with `Retry-After`. |

[Proxy architecture](../architecture/proxy.md) explains the two modes and when
to pick which.

### `tls`

HTTPS is on when both `certfile` and `keyfile` are set.

| Key | Default | Description |
|---|---|---|
| `certfile` | empty | Certificate chain (PEM). |
| `keyfile` | empty | Private key (PEM). |
| `keyfile_password` | empty | Password for an encrypted `keyfile`. |
| `ca_certs` | empty | CA bundle (PEM), passed to uvicorn as `ssl_ca_certs`. Client certificates are not required. |

### `vault`

Where sessions and their mappings are stored. See
[Sessions and vault](sessions.md).

| Key | Default | Description |
|---|---|---|
| `type` | `memory` | `memory` (lost on restart), `sqlite` (needs `privyx[sqlite]`), `redis` (needs `privyx[redis]`), or a plugin vault. |
| `dsn` | `sqlite+aiosqlite:///privyx.db` | SQLite database file. A bare path works too. |
| `redis_url` | `redis://localhost:6379/0` | Redis connection URL. A call to Redis gives up after 5 s; query parameters change that, as in `redis://localhost:6379/0?socket_timeout=2&socket_connect_timeout=2`. |
| `ttl` | unset | Expire a session after this many seconds without a request. Unset keeps sessions until they are deleted or pruned. Must be above `0`. |

### `session`

| Key | Default | Description |
|---|---|---|
| `strategy` | `ephemeral` | How to pick a session for a request with no `x-privyx-session` header: `ephemeral`, `client`, or `conversation`. See [Sessions and vault](sessions.md). |

### `detector`

What counts as sensitive. `detector` is one mapping, or a list of them whose
findings are pooled. [Detection](detection.md) covers each type with examples.

| Key | Default | Description |
|---|---|---|
| `type` | `regex` | `regex`, `yaml`, `presidio`, `llm`, or a plugin detector. |
| `patterns` | `{}` | Entity → regular expression. |
| `terms` | `{}` | Entity → list of literal strings, matched case-insensitively. |
| `cache` | `true` | Reuse detections for text seen before. `true`/`false`, or `{enabled, max_size}` (`max_size` defaults to `10000`). An entry keeps a digest of the text and what was found in it, not the text. |
| `language` | `en` | `presidio`: language of the text. |
| `model` | empty | `presidio`: spaCy model. Empty uses `{language}_core_web_sm`. |
| `entities` | `[]` | `presidio`: entity types to look for. Empty uses every recognizer. |
| `score_threshold` | `0.35` | `presidio`: minimum confidence. |
| `llm_provider` | `openai` | `llm`: `openai` or `anthropic`. |
| `llm_model` | empty | `llm`: model name. Empty uses `gpt-4o-mini` or `claude-3-5-haiku-latest`. |
| `llm_api_key` | empty | `llm`: key for that provider. Empty uses the SDK's own environment variable. |
| `llm_instructions` | empty | `llm`: replaces the built-in prompt. |
| `llm_timeout` | `30` | `llm`: seconds before a scan fails. |
| `llm_max_chars` | `4000` | `llm`: longer text is scanned in overlapping chunks. |
| `llm_fallback_on_error` | `false` | `llm`: scan with the built-in patterns when the LLM fails, instead of failing the request. |

### `policy`

Which detections are masked. See [Policy](detection.md#policy).

| Key | Default | Description |
|---|---|---|
| `type` | `default` | `default` masks everything detected; `strict` masks only the `allowed` entities; or a plugin policy. |
| `allowed` | `[]` | `strict`: entity types to mask. Empty means every built-in entity. |

### `operator`

What a detected value becomes. See [Masking](masking.md).

| Key | Default | Description |
|---|---|---|
| `type` | `pseudonym` | `pseudonym`, `hash`, `encrypt`, `faker`, `redact`, or a plugin operator. |
| `token` | `[REDACTED]` | `redact`: replacement text. |
| `length` | `12` | `hash`: hex characters of the digest kept in the token. |
| `locale` | empty | `faker`: Faker locale such as `en_US`. Empty uses Faker's default. |
| `seed` | unset | `faker`: makes the same value fake the same way across runs. |
| `key` | empty | `encrypt`: 64 hex characters (`openssl rand -hex 32`). |

### `anchor`

| Key | Default | Description |
|---|---|---|
| `type` | `hmac` | `hmac`, or a plugin anchor. |
| `secret` | empty | HMAC key. Set, pseudonyms derive from the value and stay the same across sessions; empty uses a per-session counter. See [Anchors](masking.md#anchors). |

### `token`

How placeholders look. See [Token format](masking.md#token-format).

| Key | Default | Description |
|---|---|---|
| `namespace` | `PRIVYX` | Fills `{namespace}` in `format`. |
| `format` | `<{namespace}_{type}_{id}>` | Placeholder template. Must contain `{type}` and `{id}`. |

### `audit`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Append PII-safe audit events to `path`. |
| `path` | `privyx-audit.log` | Audit log file, one JSON object per line. `privyx run` defaults to `$XDG_STATE_HOME/privyx/audit.log` (`~/.local/state/privyx/audit.log`). See [Audit events](../observability/audit-events.md). |

### `plugins`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Load the plugins in `paths`. |
| `paths` | `[]` | Directories or `.py` files to load plugins from. See [Plugins](../development/plugins.md). |

## Environment variables

| Variable | Sets |
|---|---|
| `PRIVYX_CONFIG` | Config file path |
| `PRIVYX_HOST`, `PRIVYX_PORT` | `host`, `port` |
| `PRIVYX_LOG_LEVEL`, `PRIVYX_LOGGING`, `PRIVYX_LOG_FILE` | `log_level`, `logging`, `log_file` |
| `PRIVYX_UPSTREAM_URL` | `upstream_url` and `provider.base_url` |
| `PRIVYX_API_KEY` | `provider.api_key` |
| `PRIVYX_OPENAI_API_KEY`, `PRIVYX_ANTHROPIC_API_KEY`, `PRIVYX_GOOGLE_API_KEY` | `provider.openai_api_key`, … |
| `PRIVYX_VAULT` | `vault.type` |
| `PRIVYX_VAULT_DSN` | `vault.dsn` |
| `PRIVYX_VAULT_TTL` | `vault.ttl` |
| `PRIVYX_REDIS_URL` | `vault.redis_url` |
| `PRIVYX_SESSION_STRATEGY` | `session.strategy` |
| `PRIVYX_DETECTOR_CACHE` | `detector.cache` |
| `PRIVYX_ENCRYPT_KEY` | `operator.key` |
| `PRIVYX_ANCHOR_SECRET` | `anchor.secret` |
| `PRIVYX_TOKEN_FORMAT`, `PRIVYX_TOKEN_NAMESPACE` | `token.format`, `token.namespace` |
| `PRIVYX_AUDIT_ENABLED`, `PRIVYX_AUDIT_PATH` | `audit.enabled`, `audit.path` |
| `PRIVYX_PLUGIN_PATHS` | `plugins.paths`, comma-separated |
| `PRIVYX_SSL_CERTFILE`, `PRIVYX_SSL_KEYFILE`, `PRIVYX_SSL_KEYFILE_PASSWORD`, `PRIVYX_SSL_CA_CERTS` | `tls.*` (a `PRIVYX_TLS_` prefix works too) |

Boolean variables are true for `1`, `true`, `yes`, or `on`, and false for
`0`, `false`, `no`, or `off`, in any case; any other value, like an invalid
`PRIVYX_PORT`, stops `privyx` at startup with an error naming the variable.
Settings without a variable (`proxy`, `policy`, most of `detector`) need a
config file.
