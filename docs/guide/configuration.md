---
description: Where Privyx reads its settings from, how config files, includes, environment variables, and secrets combine, and every setting with its default.
---

# Configuration

## Where settings come from

Privyx builds its settings from five layers. Each one overrides the one
before it:

1. Built-in defaults.
2. `~/.privyx/config.yaml`, your own config file, then `~/.privyx/settings.yaml`,
   the file `privyx config set` writes.
3. `privyx.yaml`, `.privyx/config.yaml`, and then `.privyx/settings.yaml` in the
   working directory, each once you have trusted it.
4. Environment variables (`PRIVYX_*`), see [Environment variables](#environment-variables).
5. Command-line flags such as `--port` or `--upstream`.

A file passed with `-c` / `--config` or named by `PRIVYX_CONFIG` is read alone,
in place of layers 2 and 3. `PRIVYX_NO_DISCOVERY=1` reads no file that was not
named. `privyx config path` prints the files in use.

A config file in the working directory can load plugins and choose the
upstream, and the directory may be a repository someone else wrote. Privyx
ignores such a file, with a line on stderr, until you run `privyx config trust`
there. What you trust is the file's content: after a change to it, run
`privyx config trust` again.

Besides your config file, `~/.privyx/` holds by default the SQLite vault
(`privyx.db`), the audit trail (`audit.log`), and the anchor secret
(`anchor.key`).

A `settings.yaml` is read after the `config.yaml` beside it, so a setting
written with `privyx config set` wins over one written by hand there.

Sections merge key by key, so a file that sets only `vault.type` keeps the
defaults for the rest of `vault`. A list replaces the default rather than
extending it. Empty environment variables are ignored, except
`PRIVYX_ANCHOR_SECRET=""`, which explicitly disables the anchor.

A config file can be split with `include`, a list of files or globs relative
to the file that names them:

```yaml
# privyx.yaml
include:
  - ./privyx.d/*.yaml     # matched files in name order; matching none is fine
  - ./team-terms.yaml     # a plain path must exist
session:
  strategy: conversation
```

The included files merge in the order listed, and the including file's own
settings go on top of them. An included file cannot have an `include` of its
own. Trusting a file covers the files it includes: after a change to any of
them, or a new file matching a glob, run `privyx config trust` again.
`privyx config path` lists each included file before the file that names it.

A value in a config file can read an environment variable, so the file
itself holds no credential and one file serves several machines:

```yaml
provider:
  api_key: ${ANTHROPIC_API_KEY}
vault:
  type: redis
  redis_url: redis://${REDIS_HOST:-localhost}:6379/0
server:
  port: ${PROXY_PORT:-8080}
```

`${NAME:-default}` takes the default when `NAME` is unset or empty. A
`${NAME}` with no default whose variable is not set fails at startup, so a
missing key never reaches the upstream as an empty string. Write `$${` for a
literal `${`. Any variable can be named, not only `PRIVYX_*` ones, and only
values are read this way, never keys.

An agent profile's `env`, `args`, and `privyx` values, including those it
inherits, are read only when that profile runs. Other commands and other
profiles leave them as written, so `privyx config show` prints them
unresolved and an unset variable there fails only the launch that needs it.
The other profile fields are read for every command, so every profile's shape
is still checked. `privyx config path` reads no variables at all.

`privyx run` adds its own default (a `conversation` session strategy) just
above the built-in ones, so your files and environment still override it.

A config with a misspelled setting, an unknown type, an invalid regex, or a
value of the wrong kind fails at startup, before any request is served, and so
does a key given twice in one mapping (the error names the key and its line). A
section whose `type` names a plugin is the exception to the first: keys Privyx
does not define there are the plugin's own options, and are passed to it.

```console
$ privyx -c broken.yaml proxy
Error: unknown setting 'detectors.regex.term' (did you mean 'terms'?)
$ privyx -c broken.yaml proxy
Error: unknown detector type 'nope'
$ privyx -c broken.yaml proxy
Error: invalid setting 'vault.ttl': Input should be greater than 0 for vault type 'memory'
```

## Secrets from a password manager

A credential setting can name where the secret is kept instead of holding it:

```yaml
provider:
  api_key: op://Private/Anthropic/credential   # 1Password, read with `op read`
anchor:
  secret: keychain://privyx/anchor              # the system keychain
```

- `op://vault/item/field` runs the [1Password CLI](https://developer.1password.com/docs/cli/)
  (`op read`), which must be installed and signed in.
- `keychain://service/account` reads the macOS Keychain, the Windows Credential
  Manager, or the Secret Service on Linux. It needs `privyx[keychain]`; store
  the entry with `keyring set privyx anchor`.

References resolve only in credential settings: those whose name ends in
`api_key`, `key`, `secret`, `password`, or `token`, and every value under
`headers` and an agent's `env`. These are the settings `privyx config show`
masks, so a resolved secret is never printed. A reference anywhere else fails
at startup. A reference in a profile's `env`, `args`, or `privyx` resolves
only when that profile runs, so an unused profile never opens the password
manager. The secret is read each time the config loads and kept in memory
only; a missing entry or a failed `op read` stops startup with the setting's
name.

`secrets` holds the options of each resolver, by scheme. The two built-in ones
take none; a resolver from a [plugin](../development/plugins.md) reads its own
under its scheme, such as `secrets.doppler.project`. `secrets` and `plugins` are
read before any reference can be resolved, so a reference in them fails at
startup.

## Checking the result

```bash
privyx config                   # a summary
privyx -c my.yaml config show # every setting as JSON, credentials masked
privyx -c my.yaml doctor        # build each component and run a request through it
```

## Editor support

Privyx publishes a [JSON Schema](https://ohp1x.github.io/privyx/schema.json)
of the config file, so an editor can complete setting names, show what a
section holds, and flag a misspelled key as you type. `privyx config schema`
prints the same schema for the installed version.

With the YAML language server, used by the
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
for VS Code and by `yamlls` in Neovim, put this line at the top of the file:

```yaml
# yaml-language-server: $schema=https://ohp1x.github.io/privyx/schema.json
```

Or map the schema to the file names once, in VS Code's `settings.json`:

```json
"yaml.schemas": {
  "https://ohp1x.github.io/privyx/schema.json": ["privyx.yaml", ".privyx/config.yaml", "privyx.d/*.yaml"]
}
```

Under a built-in `type`, a pluggable section (`detectors.<name>`, `operator`,
`vault`, ...) takes only the [settings of that type](#settings-per-type), so the
editor offers only those; under any other type, any key, since a plugin reads
options of its own.

The published schema follows the latest release; to match an older install,
save `privyx config schema` to a file and point the comment at its path.
With a config that loads plugins, `privyx config schema` also knows their
types: `type` then takes only the built-in and loaded names, and a plugin with
an [`Options`](../development/plugins.md#options) class only its own options. Save it
to a file and point the comment at that file.

## Settings per type

`vault`, `policy`, `operator`, `anchor`, `provider`, each entry of `providers`,
`detectors`, and `features` choose a component with `type`, and each built-in
type uses only some of the section's settings. An entry of `detectors` or
`features` without a `type` has its own name as the type, so `detectors.regex`
is the `regex` detector.

| Section | `type` | Settings it uses |
|---|---|---|
| `vault` | `memory` | `ttl` |
| | `sqlite` | `dsn`, `ttl` |
| | `redis` | `redis_url`, `ttl` |
| `detectors` | `regex` | `builtins`, `patterns`, `terms`, `merge_terms`, `term_variants`, `context`, `enable`, `disable`, `entropy_threshold`, `decode`, `normalize` |
| | `presidio` | `language`, `model`, `entities`, `score_threshold` |
| | `llm` | `llm_provider`, `llm_model`, `llm_api_key`, `llm_instructions`, `llm_timeout`, `llm_max_chars`, `llm_fallback_on_error` |
| `policy` | `default` | none |
| | `strict` | `allowed` |
| `operator` | `pseudonym` | `id_length`, `locale`, `seed` (`locale` and `seed` are used by [labels](masking.md#labels)) |
| | `faker` | `locale`, `seed` |
| | `fpe` | `seed` |
| | `hash` (deprecated, removed in 0.4.0; use `pseudonym` with `anchor.secret`) | `id_length` |
| | `encrypt` | `key`, `id_length` |
| | `redact` | `text` |
| | `partial` | none |
| `anchor` | `hmac` | `secret` |
| `provider`, `providers.<name>` | every built-in type | `base_url`, `api_key`, `headers`, `forward_client_auth`, `path_mapping`, `timeout`, `connect_timeout`, `max_connections` |
| `features` | `audit` | `path`, `prices` |
| | `budget` | `max_turns`, `max_tokens`, `profile` |
| | `metrics`, `tracing`, `directive`, `cache_control` | none |

`cache` and `underscore_separates_words` apply to every detector type, and
`enabled` to every entry of `detectors` and `features`.

Privyx reports a setting that a type does not use, naming the key, the type,
and the file or layer, never a value:

- A setting written in the same file as the `type`, or with no `type` chosen
  at all, such as `redis_url` without `type: redis`, is a configuration error.
  Remove it or set the type that uses it.
- `terms`, `patterns`, or `enable` from another file, for each entity that no
  detector using them receives, is a warning at startup and in
  [`privyx config lint`](cli.md#privyx-config-lint): a file above picks another type for the
  detector. This stays a warning, since the file that picks the type may not be yours to change.
  Listing the same entity again under the same key in the file above
  silences it; a pattern or an opt-in built-in of that name does not.

Other settings from a file below that the new type does not use, such as
`language` once a file above sets `type: regex`, are dropped without a warning.
So are settings from environment variables, which a container image or a
compose file sets for every type it offers: `PRIVYX_VAULT_DSN` stays unused
until `PRIVYX_VAULT=sqlite`.

With `PROJECT: [kestrel]` under `detectors.regex.terms` in
`~/.privyx/config.yaml` and `detectors: {regex: {type: presidio}}` in the
project's `privyx.yaml`:

```console
'PROJECT' in 'detectors.regex.terms' from /home/alice/.privyx/config.yaml reaches no detector that uses it, so it is not masked: detector type 'presidio' from /home/alice/app/privyx.yaml does not use it
```

## Example

```yaml
# privyx.yaml
provider: anthropic          # built in: sends the key as x-api-key, adds anthropic-version

vault:
  type: sqlite               # needs privyx[sqlite]
  dsn: /var/lib/privyx/privyx.db
  ttl: 604800                # forget sessions idle for a week

session:
  strategy: conversation

detectors:
  regex:
    terms:
      PROJECT: [bluebird, nightjar]

features:
  audit:
    path: /var/log/privyx/audit.log
```

```bash
export PRIVYX_ANTHROPIC_API_KEY=...     # keep keys out of the file
privyx -c privyx.yaml proxy --upstream https://api.anthropic.com
```

The repository's
[`configs/`](https://github.com/ohp1x/privyx/tree/main/configs) folder has
`default.yaml`, a template that lists every setting with its default value,
commented out, so loading it changes nothing; a `strict.yaml`; and small
examples, agent profiles among them. The Docker image ships them under
`/app/configs`.

## Reference

### `server`

Where `privyx proxy` listens and how many processes serve it. A file written
with the older top-level `host`, `port`, and `tls` keys is still read, with a
warning: see [Upgrading](upgrading.md).

| Key | Default | Description |
|---|---|---|
| `host` | `127.0.0.1` | Address the proxy binds to. |
| `port` | `8000` | Port the proxy listens on. |
| `workers` | `1` | Server processes. More than one needs a vault they all share: `sqlite` with a file in `dsn`, or `redis`. A feature that keeps its state in one process, such as `budget`, is refused with more than one, and each worker of `metrics` counts its own requests. `privyx run` always serves with one. |
| `tls` | see below | HTTPS settings. |

HTTPS is on when both `certfile` and `keyfile` of `tls` are set.

| Key | Default | Description |
|---|---|---|
| `tls`: `certfile` | empty | Certificate chain (PEM). |
| `tls`: `keyfile` | empty | Private key (PEM). |
| `tls`: `keyfile_password` | empty | Password for an encrypted `keyfile`. |
| `tls`: `ca_certs` | empty | CA bundle (PEM), passed to uvicorn as `ssl_ca_certs`. Client certificates are not required. |

### `log`

| Key | Default | Description |
|---|---|---|
| `level` | `info` | `debug`, `info`, `warning`, or `error`, for Privyx's own logs. |
| `format` | `text` | Console format: `text` lines or `json` objects. |
| `file` | empty | File that also receives third-party logs at `level`. At `debug` it holds raw vault rows, which include original values; it must be a regular file and is created with mode `0600`. On POSIX systems, existing files must belong to the current user and have no permissions beyond `0600`; otherwise startup fails before writing. Use `chmod 600` to fix overly broad permissions. |

### `agents`

Named launch profiles for `privyx run`. The built-in `claude`, `codex`,
`openai`, `aider`, `pi`, `opencode`, `grok`, `gemini`, and `ollama` profiles can be overridden field by field. New profiles
need a `command` or can inherit it with `extends`. Give `env_vars` so Privyx
knows where to inject its URL, or use an agent `type` that injects a CLI override.
The top-level `harvest` setting (default `false`) makes every `privyx run`
mask what its working directory tells and its local OS username, as [`--harvest`](cli.md#privyx-run) does.

```yaml
agents:
  claude-work:
    command: claude
    provider: anthropic
    upstream: https://router.example.com
    base_path: ""
    env_vars: [ANTHROPIC_BASE_URL]
```

```bash
privyx run --dry-run claude-work
privyx run claude-work
```

| Profile field | Default | Description |
|---|---|---|
| `command` | Required for a new profile | Executable name or path, passed directly to a subprocess. Pass arguments after `--`; this field is not a shell command. |
| `provider` | the config's `provider` | The named provider the profile uses, one of `providers`, such as `anthropic` or `xai`. `privyx run --provider` picks another for one run. |
| `upstream` | empty | Explicit HTTP(S) upstream URL. Without an explicit upstream, transparent mode follows supported native agent endpoints, then the provider default. Transparent mode forwards to its origin, or to the full URL with `provider.path_mapping.mode: prefix`; gateway mode uses the full URL. |
| `env_vars` | `[]` | Environment variable names to set to the local proxy URL. A configured list replaces the built-in profile's list; CLI `--env-var` adds names. Supported native endpoint values are read before injection; other variable names do not imply an upstream unless `endpoint_env` names them. |
| `endpoint_env` | that of the built-in profile named like the command's file | Environment variables that may hold the tool's own endpoint, in order. The first one set becomes the upstream when none is given explicitly (see [Where requests go](cli.md#where-requests-go)). Inherited with `extends`, so a wrapper with its own `command` follows the same variables as the tool it wraps. Read by type `env` only. |
| `base_path` | empty | Path appended to the local proxy URL, such as `/v1`. Empty or an absolute path without query or fragment. |
| `extends` | `null` | Parent profile name. Inherits command, transport, type, args, and environment settings. Unknown parents and cycles are rejected. |
| `type` | unset | The agent type that knows the tool. `env` injects only URL environment variables; `claude` also injects `--settings`; `codex` also injects `-c openai_base_url`; `pi` also injects `-e` with Privyx's pi extension; `ollama` runs `ollama run <model>` against the local server. Unset, a profile takes the type of the profile it extends, else the type named like the profile or like its command's file, otherwise `env`. The built-in profiles name theirs. A plugin can add types, whose own options are written in the profile. |
| `args` | `[]` | Default argument list, followed by the user's CLI arguments after `--`. Lists replace inherited/configured lists; `[]` clears inherited defaults. Passed without a shell; `${VAR}` config references resolve only for the selected profile. |
| `env` | `{}` | Destination environment variable name → string value; quote YAML booleans/numbers. Config references resolve only for the selected profile. Merged by name over the shell environment; values are masked in previews and config dumps. |
| `env_from` | `{}` | Destination environment variable name → source variable name in the parent shell. Resolve only for the selected launch; a missing source fails before any resource opens. An existing empty source value is allowed. Values are never expanded from profile `env`. |
| `budget` | `{}` | Limits on one launch: `max_turns` (model requests) and `max_tokens` (tokens the upstream reports, input and output). Unset means no limit. See [Budget](#budget). |
| `presets` | `[]` | Names of [presets](#presets) to apply, left to right, below this profile's `privyx` settings. A list like `args`: a child's list replaces its parent's, and `[]` clears it. |
| `privyx` | `{}` | Privyx settings for this profile only, such as `detector` or `operator`. Checked like a config file. See [Settings per profile](#settings-per-profile). |

Use inheritance to avoid copying a built-in profile and reference credentials already
present in your shell:

```yaml
agents:
  claude-work:
    extends: claude
    upstream: https://router.example.com
    args: ["--verbose"]
    env:
      MY_CUSTOM_FLAG: "enabled"
    env_from:
      ANTHROPIC_AUTH_TOKEN: WORK_ANTHROPIC_TOKEN
```

Set `WORK_ANTHROPIC_TOKEN` in your shell or secret manager before running
`privyx run --dry-run claude-work` or `privyx run claude-work`. The reference
name is visible in the preview; its value is masked. `--list` and `config
show` do not resolve references, so unused profiles need no credentials.

A credential written as is in a profile's `env` (a name such as
`*_TOKEN`, `*_KEY`, or `*_SECRET`, with no `${NAME}` in its value) in a file
that other users can read gets a warning, as ssh warns about a private key:
`chmod 600` the file, or use `env_from` or `${NAME}`.

A profile named `base:variant` is a variant of `base` when it sets no
`extends` of its own and `base` is itself a profile: it inherits as if it
said `extends: base`. Several endpoints or tokens for one tool then need no
boilerplate, and `privyx run base` still launches the plain `base`:

```yaml
agents:
  claude:work:
    upstream: https://router.example.com
    env_from: {ANTHROPIC_AUTH_TOKEN: WORK_ANTHROPIC_TOKEN}
  claude:home:
    upstream: https://home.example.com
```

Run them with `privyx run claude:work`. An explicit `extends` is always kept
as written, and may name another `:` profile (`extends: claude:work`). A
`base:variant` whose `base` is not a profile stands on its own.

Inheritance is resolved after the layers are merged, using the parent's
final configuration. Explicit child fields win over inherited fields even
if the parent was updated in a later file. Lists replace; `env` and
`env_from` merge by destination. A child can change a parent's destination
from a literal to a reference or the reverse. Defining the same destination
in both maps of one profile is rejected. Maps merge by key across config
layers; empty maps do not clear inherited entries.

Child process environment precedence is shell → profile literals/references
→ injected proxy URLs. Privyx's URL wins even if a profile also defines that
destination. Custom `PATH` is used to locate the executable. Profile values
are not exported into Privyx's own process.

#### Budget

An agent that loops, retrying a failing fix again and again, keeps sending
requests. `budget` stops a launch at a limit:

```yaml
agents:
  claude:
    budget:
      max_turns: 200          # model requests
      max_tokens: 5000000     # input + output, cached included, as the upstream reports them
```

A turn is one request on a masked chat route; token counting requests are not
turns. Tokens are summed from the `usage` of each reply. A streamed Chat
Completions reply has `usage` only when asked for, so with `max_tokens` Privyx
adds `stream_options.include_usage` to such a request unless the client set
`include_usage` itself. A reply that still comes without `usage` cannot be
counted: the next request is refused rather than counted as free. Both limits are checked
before each request, so the request that crosses one completes and the next is
refused with `400` and the error type `privyx_budget_exceeded`, which agents
show without retrying. Tokens are counted as replies end, so `max_tokens` can
be passed by as many requests as are in flight when it is reached. Privyx records a
[`run.budget_exceeded`](../observability/audit-events.md#runbudget_exceeded)
event and, when the tool exits, says which limit stopped it. A budget counts
one launch; a new `privyx run` starts from zero. `--dry-run` shows the budget.

#### Settings per profile

`privyx` holds Privyx settings for one profile: another detector, operator,
or policy for one agent, say. They take the place of the same settings in
config files and the environment, and command-line flags still win. Unknown
keys are rejected, as in a config file, for every profile, selected or not.
Maps merge with a parent's, as in `extends`.

```yaml
agents:
  claude:strict:
    privyx:
      detectors:
        regex: true
        presidio: true
      policy:
        type: strict
```

A profile without `privyx` behaves as before. `agents` and `presets` cannot
be set in it.

#### Presets

`presets` names sets of Privyx settings written once and applied to any
profile, or to one command, so a combination such as strict routing with
debug logging needs no profile of its own:

```yaml
presets:
  strict:
    proxy:
      passthrough_unknown: false
  debug:
    log:
      level: debug
      file: ${HOME}/.privyx/debug.log

agents:
  claude:work:
    upstream: https://router.example.com
    presets:
    - strict
```

```bash
privyx run claude:work                  # strict
privyx run claude:work --preset debug   # strict, and debug for this launch
privyx --preset strict --preset debug proxy
```

A preset holds settings as a config file does, without a `privyx` wrapper,
and is checked like one: unknown keys are rejected in every preset, used or
not. It cannot set `provider`, `providers`, `agents`, `presets`, or
`include`: where a profile's requests go, and with which credentials, stays
the profile's. A preset does nothing until a profile's `presets` or
[`--preset`](cli.md) names it, and a name no preset has is an error.
Variable and secret references in a preset are resolved only when it is
applied. Presets of the same name in two config files merge, as other
settings do.

For a `privyx run` launch, settings apply in this order, each above the ones
before:

1. Defaults, `privyx run` defaults, config files, and `PRIVYX_*` variables.
2. The profile's `presets`, left to right.
3. The profile's `privyx` settings.
4. `--preset`, in the order given.
5. Other command-line flags, such as `--session-strategy`.

A preset can loosen what a profile sets (`passthrough_unknown: true` over a
strict profile, say); it is allowed, and `privyx run --dry-run` names the
presets applied and each setting they decide, with sensitive values masked as
in `config show`.

Every command that reads configuration takes `--preset`; without a profile,
the presets apply above config files and the environment and below the
command's other flags, and `config show --preset NAME` shows the result.
A preset applies to the command it is given to only: an agent launched by
`privyx run`, and any `privyx` command the agent calls, does not inherit it.

For a wrapper whose filename differs from the agent's, keep the type
explicit. For example, a profile with `extends: claude` and
`command: company-claude-wrapper` inherits `type: claude`. Select
`type: env` to disable CLI injection and use only `env_vars`. Tools with
special protocols may still need a new agent type.

Profiles follow the same layers as other settings: built-in defaults, global
file, trusted project files, environment, and CLI. In each layer the selected
profile's explicit `provider` and `upstream` override the common
`provider.type` and upstream settings. Later layers win: for example,
`PRIVYX_UPSTREAM_URL` overrides a file's profile `upstream`, and `--upstream`
overrides both. In a run plan, a later layer's common `provider.base_url`
also overrides an earlier profile's upstream. Other commands do not select
profiles. `privyx config show` displays profile definitions; `privyx run
--dry-run NAME` displays the effective launch settings and their sources.

`PRIVYX_AGENTS` accepts a JSON object of partial profile overrides:

```bash
export PRIVYX_AGENTS='{"claude-work":{"upstream":"https://another.example.com"}}'
```

Lists replace previous lists. Profile field typos, malformed URLs, invalid
environment variable names, and invalid types fail at configuration load.
Project profiles follow `privyx config trust`, including renewed trust after a file
changes. API keys stay in the tool's environment or Privyx's provider settings.

The `claude` and `codex` types retain the existing argument overrides described in the
[CLI reference](cli.md#privyx-run). The same limitations about custom Codex
providers and user-supplied Claude settings apply.

### `provider` and `providers`

Where requests go and which key they carry. `providers` names upstream
connections, and `provider` selects one by name or defines one of its own:

```yaml
providers:
  work:
    type: anthropic
    base_url: https://router.example.com
    api_key: ${WORK_ROUTER_TOKEN}
provider: work
```

`providers` already holds `generic`, `openai`, `anthropic`, `google`, `xai`,
and `ollama`, so `provider: openai` needs no definition. An agent profile
selects its own with `provider`, and `--provider` picks one over both. A
credential (`api_key`, or a header) is sent only to the `base_url` of the
definition it is written in, or to its type's default URL when that writes
none; a destination from elsewhere, such as a profile's `upstream`, never takes
it along, and Privyx refuses to start rather than send it there.

A `provider` mapping holds the same keys as an entry of `providers`:

| Key | Default | Description |
|---|---|---|
| `type` | `generic` | `generic`, `openai`, `anthropic`, `google`, or a plugin provider. Sets the default upstream and how the key is sent: `anthropic` sends it as `x-api-key` and adds `anthropic-version`, `google` sends it as `x-goog-api-key`. |
| `base_url` | empty | Upstream endpoint. Empty uses the default for `type`: `http://localhost:20128` for `generic`, `https://api.openai.com/v1/chat/completions` for `openai`, `https://api.anthropic.com/v1/messages` for `anthropic`, `https://generativelanguage.googleapis.com` for `google`. |
| `api_key` | empty | Key Privyx sends upstream in place of the client's. |
| `headers` | `{}` | Extra headers sent with every upstream request. |
| `forward_client_auth` | `true` | Transparent mode: relay the client's `Authorization` / `x-api-key` / `x-goog-api-key` and `key` query parameter when Privyx holds no key. `false` drops them and `Cookie`, so only the configured key is used. An agent profile's provider takes a common `false` unless it, or the profile's `privyx.provider`, writes its own value. |
| `path_mapping.mode` | `origin` | Transparent mode only. `origin` keeps only the upstream's `scheme://host[:port]`; `prefix` keeps its path too. See [Upstream with a path](#upstream-with-a-path). |
| `path_mapping.strip_prefix` | empty | With `mode: prefix`, the start of the request path to remove before the rest is appended, such as `/v1`. `privyx run` uses the agent's `base_path`. |
| `timeout` | `300` | Seconds the upstream may take to send the next bytes of a response, or to accept the next bytes of a request. A stream runs as long as data keeps coming. Both modes. |
| `connect_timeout` | `10` | Seconds to open a connection to the upstream. |
| `max_connections` | unset | Most connections open to the upstream at once. Unset means no limit. With a limit, a request that waits more than 10 s for a free connection gets a `503` with `Retry-After`. |

The upstream is the first non-empty of `--upstream` (or `PRIVYX_UPSTREAM_URL`),
the selected provider's `base_url`, and the default for its `type`. When an
agent profile selects a named provider, that provider's own `base_url` wins
over a common `provider.base_url` in the same layer; a later layer's URL, such
as `--upstream`, still moves it. The
transparent proxy uses only its `scheme://host[:port]` and appends the client's
own path, unless [`provider.path_mapping`](#upstream-with-a-path) keeps its path
too; the gateway posts to it as written.

For `run` in transparent mode, supported native endpoint settings fill in the
destination before the provider default when neither upstream is explicit.
Profile `env` and `env_from` are included, and Claude settings are checked for
routing conflicts before launch. See [native agent endpoints](cli.md#privyx-run) for
the source order, path requirements, and `--settings` restrictions.

The key is the selected provider's `api_key`. With none set, the transparent
proxy relays the client's own key and the gateway sends none. Keys have no
command-line flag, since a command line ends up in shell history and `ps`; set
them in a file, or through `PRIVYX_API_KEY` or `PRIVYX_OPENAI_API_KEY`,
`PRIVYX_ANTHROPIC_API_KEY`, and `PRIVYX_GOOGLE_API_KEY`, which set the key of the
provider of that name.

### `proxy`

| Key | Default | Description |
|---|---|---|
| `mode` | `transparent` | `transparent` forwards every path to the upstream origin; `gateway` serves Privyx's own endpoints and posts to one fixed URL. Same as `--transparent` / `--gateway`. |
| `routes` | twenty-seven paths | Path → wire schema (`openai`, `anthropic`, `responses`, `embeddings`, `gemini`, `ollama`) for the requests Privyx masks; a `*` matches any characters except `/`. A map given here replaces the default one. See [Routes](proxy.md#routes). |
| `passthrough_unknown` | `true` | Forward paths not in `routes` unchanged. `false` answers them with `403` instead; enabled by the shipped `configs/strict.yaml`. `doctor` reports route coverage and warns on passthrough. |
| `dlp_check` | `true` | Mask every copy of a value a request masked wherever it stands in text Privyx masks. Then, before the request goes out, look for every value it masked anywhere else in it (fields that are not masked, URLs, headers other than the credential). One found answers `422` and the request is not forwarded. Only exact signed thinking remembered from an upstream response is reinstated after the check. See [Leak check](masking.md#leak-check). |
| `tap` | `false` | Record each masked exchange before and after masking, for `privyx audit watch`. The files hold original values. See [Session tap](../observability/tap.md). |
| `tap_dir` | `~/.privyx/tap` | Where the tap writes, one file per session. |

The upstream connection (`timeout`, `connect_timeout`, `max_connections`,
`path_mapping`, `forward_client_auth`) is set on the [provider](#provider-and-providers).
`directive` and `cache_control` are [features](#features).

[Proxy modes and routes](proxy.md) explains the two modes and when to pick
which.

#### Upstream with a path

A router or provider whose API sits under a path, such as
`https://router.example.com/api/v1`, needs `mode: prefix` in the provider's
`path_mapping`. The
transparent proxy then keeps the upstream's path and appends the request path
after `strip_prefix`:

```yaml
provider:
  path_mapping:
    mode: prefix
    strip_prefix: /v1
  base_url: https://router.example.com/api/v1
```

```text
client  → http://localhost:8000/v1/chat/completions
Privyx  → https://router.example.com/api/v1/chat/completions  (masked)
```

- Masking still picks its [route](proxy.md#routes) from the request path, before
  the prefix is removed.
- `strip_prefix` is removed only at a segment boundary: `/v1` takes
  `/v1/models`, not `/v10/models`. Empty removes nothing.
- A request outside `strip_prefix`, or one with a `.` or `..` segment, a
  backslash, or an encoded `/`, is answered `404` and not forwarded.
- The query is forwarded as before.
- `prefix` needs an explicit upstream (`--upstream`, or `provider.base_url`) without query, fragment, or credentials, and is refused
  in gateway mode.

`privyx run` removes the agent's `base_path`, since the tool's base URL is the
proxy plus `base_path` and stands for the upstream URL. With prefix mode, a
native endpoint with a path of its own is followed rather than refused. For a
profile:

```yaml
agents:
  codex:router:
    extends: codex
    upstream: https://router.example.com/api/v1
    privyx:
      provider:
        path_mapping:
          mode: prefix
```

### `vault`

Where sessions and their mappings are stored. See
[Sessions and vault](sessions.md). With `server.workers` above `1`, use `sqlite` or `redis`.

| Key | Default | Description |
|---|---|---|
| `type` | `memory` | `memory` (lost on restart), `sqlite` (needs `privyx[sqlite]`), `redis` (needs `privyx[redis]`), or a plugin vault. |
| `dsn` | `sqlite+aiosqlite:///~/.privyx/privyx.db` | SQLite database file: a bare path, `sqlite+aiosqlite:///PATH`, or `sqlite:///PATH`. `~` is your home directory. `:memory:`, or an empty path, is a database that goes with the vault, which `server.workers` above `1` refuses. |
| `redis_url` | `redis://localhost:6379/0` | Redis connection URL. A call to Redis gives up after 5 s; query parameters change that, as in `redis://localhost:6379/0?socket_timeout=2&socket_connect_timeout=2`. |
| `ttl` | unset | Expire a session after this many seconds without a request. Unset keeps sessions until they are deleted or pruned. Must be above `0`. |

### `session`

| Key | Default | Description |
|---|---|---|
| `strategy` | `ephemeral` | How to pick a session for a request with no `x-privyx-session` header: `ephemeral`, `client`, or `conversation`. See [Sessions and vault](sessions.md). |

### `detectors`

What counts as sensitive. `detectors` maps a name to a detector; their
findings are pooled, and they run in the order written. An entry without a
`type` has its name as the type, so the default is one `regex` detector.
[Detection](detection.md) covers each type with examples.

```yaml
detectors:
  regex:
    terms:
      PERSON: [john smith, alice]
    patterns:
      EMPLOYEE_ID: '\bEMP-\d{6}\b'
  presidio:
    language: en
```

Write `true` or `false` in place of an entry, or `enabled: true` or `false`
in it, to turn a detector on or off without the options a file below gave it:
`regex: false` turns the built-in detector off, `presidio: true` adds that one.
A second detector of one type needs a `type`, such as `names: {type: regex}`.
A detector that used the old `yaml` type is a `regex` detector with
`builtins: false`. Every entry takes these, besides the options of its type:

| Key | Default | Description |
|---|---|---|
| `type` | the entry's name | `regex`, `presidio`, `llm`, or a plugin detector. |
| `enabled` | `true` | Whether the detector runs. |
| `underscore_separates_words` | `true` | `_` separates words, so `terms` and the built-in patterns also match inside `ACME_API_KEY`, `DB_JOHN`, or `host_10.0.0.1`. The [leak check](masking.md#leak-check) follows it. |
| `cache` | `true` | Reuse detections for text seen before. `true`/`false`, or `{enabled, max_size}` (`max_size` defaults to `10000`). An entry keeps a digest of the text and what was found in it, not the text. |

The options of a `regex` detector:

| Key | Default | Description |
|---|---|---|
| `builtins` | `true` | Add the built-in patterns under your own. `false` keeps only your `patterns` and `terms` and the opt-in entities `enable` names. |
| `patterns` | `{}` | Entity → regular expression. |
| `terms` | `{}` | Entity → list of literal strings, matched case-insensitively. |
| `merge_terms` | `true` | Match both `terms` and `patterns` for the same entity, trying terms first. With `false`, the regex replaces the terms with a warning. |
| `term_variants` | `true` | `terms` of several words also match their other spellings: `Acme Corp` as `acme-corp`, `acme_corp`, `acmecorp`, …. |
| `context` | `{}` | Entity → words, one of which must be within five words of a match for it to count. |
| `enable` | `[]` | Opt-in built-in entities to turn on: `HIGH_ENTROPY`, `ASSIGNED_SECRET`, `MRN`, `ICD10`. |
| `disable` | `[]` | Built-in entities to turn off, including inactive opt-in entities (a no-op). A name in both `enable` and `disable`, or an unknown name, is an error. Your own `patterns` and `terms` still apply. See [Turning a built-in off](detection.md#turning-a-built-in-off). |
| `entropy_threshold` | `3.0` | `HIGH_ENTROPY`: bits per character a string needs. |
| `decode` | `false` | Also find values split into string literals (`'s' + 'k' + …`) or encoded as Base64, hex, or URL escapes. |
| `normalize` | `false` | Also find values written with fullwidth or other look-alike compatibility characters, or with zero-width characters inside (`ｊｏｈｎ`). See [Look-alike characters](detection.md#look-alike-characters). |

The options of the other types:

| Key | Default | Description |
|---|---|---|
| `language` | `en` | `presidio`: language of the text. |
| `model` | empty | `presidio`: spaCy model. Empty uses `{language}_core_web_sm`. |
| `entities` | `[]` | `presidio`: entity types to look for. Empty uses every recognizer. |
| `score_threshold` | `0.35` | `presidio`: minimum confidence. |
| `llm_provider` | `openai` | `llm`: `openai` or `anthropic`. |
| `llm_model` | empty | `llm`: model name. Empty uses `gpt-4o-mini` or `claude-haiku-4-5`. |
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
| `allowed` | `[]` | `strict`: entity types to mask. Empty means every built-in entity, opt-in ones included. |

### `operator`

What a detected value becomes. See [Masking](masking.md).

| Key | Default | Description |
|---|---|---|
| `type` | `pseudonym` | `pseudonym`, `hash`, `encrypt`, `faker`, `fpe`, `partial`, `redact`, or a plugin operator. |
| `text` | `[REDACTED]` | `redact`: replacement text. |
| `id_length` | `16` (`12` for `hash`) | `pseudonym`, `hash`, `encrypt`: hex characters in the id of an anchored `pseudonym` or an `encrypt` token, or the digest kept by `hash`, 6 to 64; another value is an error. A counter is unaffected. A value written beside `hash` does not carry to a type picked in a later layer, nor one written for the other two to `hash`. See [Id length](masking.md#id-length). |
| `locale` | empty | `faker`, and `pseudonym` [labels](masking.md#labels): Faker locale such as `en_US`. Empty uses Faker's default. |
| `seed` | unset | `faker`, `fpe`: makes the same value fake the same way across runs. Another seed gives `pseudonym` [labels](masking.md#labels) other values. |
| `key` | empty | `encrypt`: 64 hex characters (`openssl rand -hex 32`). |

### `anchor`

| Key | Default | Description |
|---|---|---|
| `type` | `hmac` | `hmac`, or a plugin anchor. |
| `secret` | `auto` | HMAC key. `auto` creates or reuses the per-user key in `~/.privyx/anchor.key`, shared by every command. An explicit key derives the same stable tokens on other machines; empty uses a per-session counter. See [Anchors](masking.md#anchors). |

### `token`

How placeholders look. See [Token format](masking.md#token-format).

| Key | Default | Description |
|---|---|---|
| `namespace` | `PRIVYX` | Fills `{namespace}` in `format`. |
| `format` | `<{namespace}_{type}_{id}>` | Placeholder template. Must contain `{type}` and `{id}`. |
| `compact` | `false` | Issue short tokens such as `<E~1>`. Tokens in `format` are still restored. See [Compact tokens](masking.md#compact-tokens). |
| `label_format` | empty | Write `pseudonym` tokens with a fake value for the model to read, such as `<{namespace}_{type}_{id}:{label}>`; empty is off. Must contain `{id}` and `{label}`. Needs `privyx[faker]`, the `pseudonym` operator, and `compact: false`. See [Labels](masking.md#labels). |

### `features`

The optional behaviors, each on or off; `privyx features list` shows the state.
Write `true` or `false` in place of an entry, or `enabled`, and the options of
the feature beside it. A plugin can add features, written the same way.

| Feature | Default | What it does |
|---|---|---|
| `audit` | on | Append PII-safe audit events to a file. See [Audit events](../observability/audit-events.md). |
| `metrics` | on | Count what the proxy does, for `GET /metrics`. It follows the config at start. |
| `tracing` | off | Export an OpenTelemetry span per proxied request. Needs `pip install 'privyx[otel]'`; the exporter is set by the `OTEL_*` variables. See [Tracing](../observability/tracing.md). |
| `directive` | off | Add a short instruction to each masked chat request, telling the model to keep placeholders exactly as written. About 80 tokens. See [Placeholder directive](masking.md#placeholder-directive). |
| `cache_control` | off | Mark a large `tools` and `system` prefix of an Anthropic request for prompt caching, when the request marks nothing itself. See [Prompt caching](proxy.md#prompt-caching). |
| `budget` | off | Limits on one `privyx run` launch; see [Budget](#budget). |

| Key | Default | Description |
|---|---|---|
| `audit`: `path` | `~/.privyx/audit.log` | Audit log file, one JSON object per line; `~` is your home directory. It is created with mode `0600`. |
| `audit`: `prices` | `{}` | Model-id prefix → prices in USD per million tokens: `input`, `output`, and optionally `cache_read` (default a tenth of `input`) and `cache_write` (default 1.25 times `input`). For the cost `privyx session show` estimates; adds models to the built-in Claude prices or overrides them. |
| `budget.max_turns`, `budget.max_tokens` | unset | The limits. Unset means no limit. |
| `budget.profile` | empty | The agent profile launched, named in the audit trail and in the refusal. |

```yaml
features:
  audit:
    path: /var/log/privyx/audit.log
    prices:
      gpt-5:
        input: 1.25
        output: 10.0
        cache_read: 0.125
  directive: true
  tracing: false
```

`privyx run` sets `budget` itself from the profile's [`budget`](#budget), with
the profile's name in `profile`; it is not written by hand. `budget` keeps its
state in one process, so it is refused with `server.workers` above `1`. The
session tap, which records each masked exchange, is `proxy.tap` and
`proxy.tap_dir`.

### `plugins`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Load the plugins in `paths` and `modules`. |
| `paths` | `[]` | Directories or `.py` files to load plugins from. See [Plugins](../development/plugins.md). |
| `modules` | `[]` | Importable modules, such as installed packages, whose `setup(app)` adds components, routes, and hooks. Experimental. See [Plugins from a package](../development/plugins.md#plugins-from-a-package). |

## Environment variables

Every setting has a variable: `PRIVYX_`, in capitals, and its path with `__`
between the names, in any case. `PRIVYX_SERVER__PORT=9000` sets `server.port`,
`PRIVYX_FEATURES__DIRECTIVE=true` turns the directive on, and
`PRIVYX_DETECTORS__REGEX__TERMS='{PERSON: [alice]}'` sets the terms of the
`regex` detector. The value is read as the setting's type: `8001` is a number,
`true` a boolean, and a list or mapping is written in YAML flow form, such as
`[a, b]` or `{a: 1}`. `PRIVYX_TOKEN__NAMESPACE=NO` stays the string `NO`.

A name must be a setting, or an entry the config already has (a detector, a
profile, an entity): a new entry is written in a file or with
`privyx config set`. A misspelled name stops startup with an error that names
it. An option follows the type in use, also a type set in the environment:
with `PRIVYX_VAULT=sqlite`, `PRIVYX_VAULT__DSN` is a setting. The variables
below are the names of earlier versions, kept as aliases; the Docker image and
`docker-compose.yml` take either name. Given an alias and the generic name of
one setting with different values, Privyx refuses the config and names both.
For the old file keys, see [Upgrading](upgrading.md).

| Variable | Sets |
|---|---|
| `PRIVYX_CONFIG` | Config file path |
| `PRIVYX_NO_DISCOVERY` | When true, no config file is read unless it is named |
| `PRIVYX_HOST`, `PRIVYX_PORT` | `server.host`, `server.port` |
| `PRIVYX_LOG_LEVEL`, `PRIVYX_LOGGING`, `PRIVYX_LOG_FILE` | `log.level`, `log.format`, `log.file` |
| `PRIVYX_UPSTREAM_URL` | `upstream_url`, the destination that wins over the provider's `base_url`; the same as `--upstream`. A config file does not write it |
| `PRIVYX_PROVIDER` | The provider selected: the name of one of `providers`; overrides a file's selected agent provider |
| `PRIVYX_AGENTS` | JSON object of agent profile overrides, merged field by field |
| `PRIVYX_API_KEY` | `api_key` of the selected provider |
| `PRIVYX_OPENAI_API_KEY`, `PRIVYX_ANTHROPIC_API_KEY`, `PRIVYX_GOOGLE_API_KEY` | `providers.openai.api_key`, `providers.anthropic.api_key`, `providers.google.api_key` |
| `PRIVYX_VAULT` | `vault.type` |
| `PRIVYX_VAULT_DSN` | `vault.dsn` |
| `PRIVYX_VAULT_TTL` | `vault.ttl` |
| `PRIVYX_REDIS_URL` | `vault.redis_url` |
| `PRIVYX_SESSION_STRATEGY` | `session.strategy` |
| `PRIVYX_DETECTOR_CACHE` | `cache` of every detector |
| `PRIVYX_ENCRYPT_KEY` | `operator.key` |
| `PRIVYX_ANCHOR_SECRET` | `anchor.secret` (an explicit empty value disables HMAC anchoring) |
| `PRIVYX_ANCHOR_KEY_PATH` | Where `anchor.secret: auto` keeps the per-user key, `~/.privyx/anchor.key` by default; an absolute path, `~` is expanded |
| `PRIVYX_TOKEN_FORMAT`, `PRIVYX_TOKEN_NAMESPACE` | `token.format`, `token.namespace` |
| `PRIVYX_AUDIT_ENABLED`, `PRIVYX_AUDIT_PATH` | `features.audit.enabled`, `features.audit.path` |
| `PRIVYX_PLUGIN_PATHS` | `plugins.paths`, comma-separated |
| `PRIVYX_PLUGIN_MODULES` | `plugins.modules`, comma-separated |
| `PRIVYX_SSL_CERTFILE`, `PRIVYX_SSL_KEYFILE`, `PRIVYX_SSL_KEYFILE_PASSWORD`, `PRIVYX_SSL_CA_CERTS` | `server.tls.*` (a `PRIVYX_TLS_` prefix works too) |

Boolean variables are true for `1`, `true`, `yes`, or `on`, and false for
`0`, `false`, `no`, or `off`, in any case; any other value, like an invalid
`PRIVYX_PORT`, stops `privyx` at startup with an error naming the variable.
