# CLI reference

Every command that reads configuration takes `-c` / `--config FILE`, which
defaults to `PRIVYX_CONFIG`. Without either, Privyx reads
`~/.privyx/config.yaml` and the trusted config files of the working directory.
See [Configuration](configuration.md). `privyx --version` prints the version.

## `privyx proxy`

Start the privacy proxy.

```bash
privyx proxy [-c FILE] [--host HOST] [--port PORT] [-u URL] [--transparent | --gateway] [--reload]
```

| Option | Description |
|---|---|
| `--host` | Bind address. Default `127.0.0.1`. |
| `--port` | Bind port. Default `8000`. |
| `-u`, `--upstream` | Upstream provider URL. Wins over the config file. |
| `--transparent` / `--gateway` | Forward every path to the upstream origin, or serve the chat-only gateway. Default: `proxy.mode`, which is `transparent`. |
| `--reload` | Restart the server when a config file in use changes. For development; an in-memory vault starts empty after each restart. |
| `--ssl-certfile`, `--ssl-keyfile` | Serve HTTPS with this certificate and key (PEM). Both are required. |
| `--ssl-keyfile-password` | Password for an encrypted key. |
| `--ssl-ca-certs` | CA bundle (PEM). |

Besides the proxied paths, the server answers `GET /health` and `GET /metrics` (Prometheus text), neither of which needs a
key.

On SIGTERM or Ctrl-C, and on a `--reload` restart, requests still running get
5 s to finish. Longer ones, such as a long streamed reply, are then cut off:
the client sees the connection close, and their ephemeral sessions are deleted.

## `privyx run`

Start a proxy on a local port, run a tool against it, and stop the proxy when
the tool exits. The exit code is the tool's. The proxy follows `proxy.mode`,
like `privyx proxy`. The transparent proxy (the default) relays the tool's own
key or login unless Privyx has a key of its own
([Configuration](configuration.md)). It masks the chat paths in `proxy.routes`
and forwards any other path unmasked, file uploads for instance;
`proxy.passthrough_unknown: false` answers those with `403` instead. With
`proxy.mode: gateway` in the config
file, Privyx sends only its own key and posts to the upstream URL as written,
which an upstream behind a base path needs. The tool owns the terminal, so
Privyx writes no log lines to it; they go to `log_file` when one is set, which
is where to look when a request fails with a `503`.

In an interactive POSIX terminal, Privyx starts the proxy in a separate process
and replaces the launcher with the tool. The tool keeps terminal input and job
control, including Ctrl-C and Ctrl-Z. The proxy stops after the tool exits;
its warnings then follow the shell's prompt, on a line of their own.
Without a controlling terminal, or on other platforms, Privyx supervises the
tool as a subprocess.

Privyx sends the agent profile name as the initial terminal title before launch;
the agent can replace it with its own session title. To let VS Code detect the
agent's process without changing editor settings, run `privyx run codex` from
the activated environment, or `.venv/bin/privyx run codex`.

With `uv run`, `uv` remains the foreground process group leader, so VS Code
tabs configured to show only the process name can still show the wrapper.
If you prefer to keep `uv run`, you can display the emitted title by setting
this in VS Code's user or workspace settings:

```json
{
  "terminal.integrated.tabs.title": "${sequence}"
}
```

```bash
privyx run [OPTIONS] TARGET [ARGS]...
privyx run claude
privyx run claude -- --continue      # arguments after -- go to the tool
privyx run -u https://api.example.com --env-var MY_TOOL_BASE_URL -- my-tool --flag
```

| Option | Description |
|---|---|
| `-p`, `--provider` | Provider type upstream (`openai`, `anthropic`, `generic`). Known targets set it for you. |
| `-u`, `--upstream` | Upstream URL. The transparent proxy uses only its origin and appends the tool's own paths; the gateway posts to it as written. Default: the provider type's default. |
| `--port` | Proxy port. `0` (default) picks a free one. |
| `--env-var` | Also set this environment variable to the proxy URL. Repeatable. |
| `--session-strategy` | `ephemeral`, `client`, or `conversation` (default). Overrides the config. |
| `--no-anchor` | Do not create an anchor secret in `~/.privyx/anchor.key`. |
| `--list` | List the known targets and exit. |
| `--dry-run` | Print the launch plan and winning configuration sources, then exit. Opens no port, starts no tool, imports no plugins, and creates no log, vault, or anchor file. Works even if the executable is not installed. |

Known targets, and where each gets the proxy's URL:

| Target | Provider | Proxy URL passed as |
|---|---|---|
| `claude` | `anthropic` | `ANTHROPIC_BASE_URL`, and the same variable in `--settings`, which outranks a base URL in Claude Code's own `settings.json` |
| `codex` | `openai` | `-c openai_base_url=…/v1` (codex does not read `OPENAI_BASE_URL`) and, for a custom provider, `-c model_providers.<id>.base_url=…/v1`, said again after your arguments when they hold a `-c` of their own, and `OPENAI_BASE_URL` |
| `openai` | `openai` | `OPENAI_BASE_URL`, ending in `/v1` as the OpenAI SDK expects |
| `aider` | `openai` | `OPENAI_API_BASE`, `OPENAI_BASE_URL`, ending in `/v1` |
| `pi` | `anthropic` | `-e` with an extension Privyx ships, which sets the `baseUrl` of the pi provider named in `PRIVYX_PI_PROVIDER` (`anthropic`) to `PRIVYX_PI_BASE_URL` |

pi has no base-URL variable or flag; the extension changes its provider for
this run only, never `models.json`. Only that one provider goes through
Privyx, so pi refuses a model of any other provider at startup, puts the
previous model back when `/model` picks one, and does not send a prompt to
it. For another provider, set `PRIVYX_PI_PROVIDER` and the upstream in a
profile (`pi:deepseek` in [Coding agents](../tutorials/coding-agents.md)).
The provider's base URL must have no path of its own, since the
transparent proxy keeps only the upstream's origin: groq, openrouter, and
other providers under a base path call paths outside `proxy.routes`, which
are forwarded unmasked.

Claude Code keeps only the last `--settings`: one of your own, after `--`,
replaces Privyx's. Codex drops every `-c` given before a subcommand once one
follows it (`exec -c …`), which is why Privyx repeats its own after your
arguments. `openai_base_url` applies to codex's built-in `openai`
provider. When codex's config selects a `model_provider` of your own, Privyx
also points that provider's `base_url` at the proxy, for this launch only; the
provider keeps its headers and authentication. Unless you set an upstream,
Privyx forwards to the provider's own `base_url`, which must end in `/v1`.
Privyx reads `/etc/codex/config.toml`, `$CODEX_HOME/config.toml`, a `-p`
profile, and `-c` overrides; a provider set only in a project's
`.codex/config.toml` is not detected. If your Claude Code settings point
`ANTHROPIC_BASE_URL` at a router or another proxy, give that URL to
`privyx run` as `--upstream`: Privyx now takes that place and forwards to its
own upstream. If the tool exits without having sent Privyx a single request,
`privyx run` warns and records a
[`run.unproxied`](../observability/audit-events.md#rununproxied) audit event: a
tool that calls its provider directly is not masked.

Any other command runs too, given `--env-var` so Privyx knows how to point it
at the proxy. The variable gets the proxy's URL with no path.

You can also define [agent profiles](configuration.md#agents) in your config
and use their names as `TARGET`. `--list` includes those profiles. Built-in
profiles keep their defaults for any fields you leave out.

Inspect a plan before launching:

```console
$ privyx run --dry-run codex
Profile: codex
Command: codex -c 'openai_base_url="http://127.0.0.1:<auto>/v1"'
Adapter: codex
Proxy: http://127.0.0.1:<auto>/v1 (transparent)
Provider: openai [defaults]
Upstream: https://api.openai.com/v1/chat/completions [provider default (defaults)]
Routing: tool → http://127.0.0.1:<auto>/v1 → https://api.openai.com
Session: conversation [run defaults]
Anchor: auto-provision on launch [run defaults]
Source command: defaults
Source base_path: defaults
Source adapter: defaults
Source args: defaults
Source host: defaults
Source proxy.mode: defaults
Source run.port: run defaults
Source arguments: CLI
Environment injected:
  OPENAI_BASE_URL=http://127.0.0.1:<auto>/v1 [defaults]
```

`<auto>` means a free port will be chosen on launch; `--port` makes the plan
use a fixed port. Sources are `defaults`, `run defaults`, a config file's
absolute path, `environment`, or `CLI`. Inherited fields retain their parent's
source. The environment section lists proxy URL variables plus masked custom
values and the source names of `env_from` references. Credential flags, JSON settings,
config assignments, URL user info, queries and fragments are masked in the
display; arbitrary free-text arguments are displayed as supplied. The plan
checks configuration, but does not verify that the agent sends requests
through the proxy. The same arguments and resolved settings are used when
you launch without `--dry-run`. Both paths consume the same launch plan,
including an environment snapshot; binding a free port changes only its
proxy URL. Missing environment references fail before startup.

## `privyx detect`

Show what the configured detector and policy find in a text.

```bash
privyx detect [-c FILE] [--transform] [--no-policy] [--explain] TEXT...
echo "mail alice@example.com" | privyx detect --stdin
```

| Option | Description |
|---|---|
| `--transform` | Also print the masked text. |
| `--policy` / `--no-policy` | Apply the configured policy (default) or show every detection. |
| `--explain` | For each span, the detector and rule that found it (a built-in pattern, your `patterns`, or your `terms`) and whether the policy masks all of it, part of it, or none. |
| `--stdin` | Read the text from standard input. |

`--explain` helps when a value is masked that should not be, or the reverse:

```console
$ privyx detect --explain "card 4111 1111 1111 1111"
     5:24    CREDIT_CARD       '4111 1111 1111 1111'
        found by  regex: built-in pattern PHONE
        found by  regex: built-in pattern CREDIT_CARD
        masked
```

With several detectors configured, each is numbered in the order listed.

## `privyx mask`

Replace sensitive values with tokens, reversibly. The output is what the proxy
would send upstream.

```bash
privyx mask --map map.json "mail alice@example.com"
privyx mask --map map.json -i request.json --path '$.messages'
cat events.jsonl | privyx mask --map map.json -f jsonl -i - -o masked.jsonl
```

| Option | Description |
|---|---|
| `--map FILE` | Read and write the mapping in this file. An existing file is extended, so tokens stay the same across documents. |
| `--session ID` | Keep the mapping in this vault session instead. Needs a persistent vault. |
| `-i`, `--input FILE` | Input file; `-` for standard input. |
| `--stdin` | Read from standard input. |
| `-o`, `--output FILE` | Output file. Default: standard output. |
| `-f`, `--format` | `auto` (default, from the file extension), `text`, `json`, or `jsonl`. |
| `--path` | Only mask under this JSON path, e.g. `$.messages`. Repeatable. |

## `privyx unmask`

Put the original values back, from a `--map` file or a `--session`. Tokens the
mapping does not know are left as they are. Takes the same options as
`privyx mask`.

```bash
privyx unmask --map map.json -i masked.txt
```

## `privyx session`

List, show, and delete vault sessions. These need a `sqlite` or `redis` vault;
see [Sessions and vault](sessions.md).

```bash
privyx session list
privyx session show SESSION_ID [--reveal]
privyx session prune --older-than 7d [--dry-run]
```

| Command | Option | Description |
|---|---|---|
| `list` | | Sessions, most recently active first, with mapping counts. Never shows values. |
| `show` | `--reveal` | Print original values in full. Without it they are masked. |
| `prune` | `--older-than` | Required. Delete sessions idle at least this long: `30m`, `12h`, `7d`, `2w`. |
| `prune` | `--dry-run` | List what would be deleted and delete nothing. |

## `privyx inspect`

Look into what Privyx does with a request, a pattern, or a token.

```bash
privyx inspect routes                     # which paths are masked, and with which schema
privyx inspect regex EMPLOYEE_ID          # a configured entity's pattern
privyx inspect regex '\bEMP-\d{6}\b'      # or a pattern you are writing
privyx inspect token '<PRIVYX_EMAIL_1>'   # the session a token belongs to
```

| Command | Option | Description |
|---|---|---|
| `routes` | | The serving mode, the upstream, each path in `proxy.routes` with its wire schema, and what happens to any other path. |
| `regex` | | Whether the pattern can backtrack catastrophically, and how fast it scans ordinary text. A configured entity name, built-in ones included, stands for its pattern. The timing stops after 5 seconds and reports the pattern as stalled, which also catches backtracking the static check misses. |
| `token` | `--reveal` | The token's type, namespace, and id, then each session that holds it, with its creation, last activity, and expiry. The original value is masked unless `--reveal` is given. |

`privyx inspect session SESSION_ID` is an older name for `privyx session show`.

## `privyx audit`

Read the audit trail. `FILE` defaults to `audit.path`, which is
`~/.privyx/audit.log` unless you set one.

```bash
privyx audit stats [FILE] [--since 24h]
privyx audit tail [FILE] [-n 20] [--no-follow]
```

| Command | Option | Description |
|---|---|---|
| `stats` | `--since` | Only count events from the last `30m`, `12h`, `7d`, `2w`. |
| `tail` | `-n`, `--lines` | Recent events to show first. Default `10`. |
| `tail` | `--follow` / `--no-follow` | Keep printing new events (default) or stop. |

See [Audit events](../observability/audit-events.md) for what each event holds.

## `privyx config`

Show the configuration.

```bash
privyx config            # a summary
privyx config --show     # every setting as JSON, credentials masked
privyx config --path     # the config files in use, or "defaults"
privyx config --schema   # the JSON Schema of a config file
```

## `privyx trust`

Trust the config files of the working directory: `privyx.yaml` and
`.privyx/config.yaml`.

```bash
privyx trust
```

Such a file can load plugins and choose the upstream, so Privyx reads one only
after this command, and says so on stderr while it ignores one. Trust is kept
in `~/.privyx/trusted.json` for the file's path and content: run the command
again after you change the file. While you are still editing one, pass it with
`-c`, which needs no trust.

## `privyx scan`

Find the names and secrets a workspace should keep from the model, and add the
ones you keep to `privyx.d/terms.yaml`.

```bash
privyx scan [PATH]          # list, ask what to leave out, then add the rest
privyx scan --dry-run       # only list
privyx scan --yes           # add everything found
```

| Option | Description |
|---|---|
| `-y`, `--yes` | Add everything found, without asking. |
| `--dry-run` | List what was found and write nothing. |

It reads, in `PATH` (default: the working directory):

- the git history: each author and `Co-authored-by` name, as `PERSON` (bots left out);
- the git remotes: the owner as `ORGANIZATION` and the repository as `PROJECT`, never the host or a credential in the URL;
- `pyproject.toml`, `package.json`, `Cargo.toml`, and `go.mod`: the project name, and an npm scope as `ORGANIZATION`;
- `.env` files, except those ending in `.example`, `.sample`, `.template`, or `.dist`: each key whose value is six characters or longer, neither a number nor a single word (`true`, `development`), and not already masked by the built-in detector, as a pattern `ENV_<KEY>` that masks the value assigned to that key (`KEY=…`, `KEY: …`, `"KEY": "…"`). The values themselves are never written.

```console
$ privyx scan
Found in /home/ana/billing:
    1  PERSON          Ana Example  (git history)
    2  ORGANIZATION    acme-labs  (git remote origin, package.json)
    3  PROJECT         billing  (git remote origin)
    4  ENV_SENTRY_DSN  value of SENTRY_DSN  (.env)
Numbers to leave out (e.g. 2,5), or Enter to keep all:
Added 4 to /home/ana/billing/privyx.d/terms.yaml
Created /home/ana/billing/privyx.yaml, which includes it
```

The file only grows: a later scan lists only what it does not hold yet, so a
scan that finds nothing new changes nothing. What you left out is offered
again. Without a `privyx.yaml`, the scan creates one that includes the file and
trusts it, since it wrote both. An existing `privyx.yaml` is not changed: add
`privyx.d/terms.yaml` to its `include` list. It stays trusted when it was
trusted before; otherwise run `privyx trust` after reviewing it. A `detector`
given as a list in `privyx.yaml` replaces the terms it includes; move them into
the list.

## `privyx doctor`

Build every configured component and push a request through it: plugins,
detector, vault (a real write and read), provider, proxy, and streaming
restore. The `routes` check lists masking paths, gaps in the default route map,
and the disposition of unlisted paths. Passthrough prints a warning; invalid
routes fail. Exits with a non-zero status if any check fails.

```console
$ privyx doctor
Privyx Doctor
=============
  ✓ plugins: no plugin paths configured
  ✓ detector: regex: 1 span(s) ['EMAIL']
  ✓ permissions: sensitive files owner-only (0600)
  ✓ vault: memory: round-trip ok
  ✓ provider: generic → http://localhost:20128
  ✓ proxy: request transformed, 1 mapping(s) vaulted
  ✓ streaming: pseudonym restored across a split chunk
  ✓ routes: transparent: 10 masking route(s): ...; WARNING: unlisted paths pass through WITHOUT body masking; set proxy.passthrough_unknown: false to return 403; default paths outside coverage: none
  ✓ configuration: valid: detector=regex policy=default operator=pseudonym vault=memory anchor=off
```
