# CLI reference

Use `privyx -c FILE <command>` (or `--config FILE`) to select configuration
for any command, including nested commands and `hook`:

```bash
privyx -c config.yaml mask "alice@example.com"
privyx -c config.yaml inspect regex EMAIL
privyx -c config.yaml run codex
```

Existing `-c` / `--config` options on subcommands still work. Precedence is:
subcommand `-c` > global `-c` > `PRIVYX_CONFIG` > config discovery.
A subcommand override wins without a warning. Without an explicit file, Privyx reads
`~/.privyx/config.yaml` and the trusted config files of the working directory.
See [Configuration](configuration.md). `privyx --version` prints the version.

The global option sets `PRIVYX_CONFIG` to an absolute path for the duration of
the command. Child processes inherit it, including agents launched by `run`;
their own Privyx commands use that file unless overridden. A subcommand's `-c`
does not replace that inherited global value. Options after the agent name
belong to the agent: `privyx -c config.yaml run codex -c ...` passes the second
`-c` to Codex. Commands that do not read configuration ignore the global option.

Every command that takes `-c` also takes `--preset NAME`, repeatable, to apply
a [preset](configuration.md#presets) for that command only:

```bash
privyx proxy --preset strict
privyx config --sources --preset debug
privyx run claude:work --preset debug
```

Unlike the global `-c`, a preset is not passed to child processes: an agent
launched by `run`, and any Privyx command it calls, such as an MCP server or
a hook, does not inherit it.

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
which one fixed endpoint URL needs. An upstream under a base path also works
in transparent mode with
[`proxy.path_mapping`](configuration.md#proxy). The tool owns the terminal, so
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
privyx run -p openai my-tool         # OPENAI_BASE_URL and OPENAI_API_BASE
privyx run -u https://api.example.com --env-var MY_TOOL_BASE_URL -- my-tool --flag
```

| Option | Description |
|---|---|
| `-p`, `--provider` | Provider type upstream (`openai`, `anthropic`, `generic`). Known targets set it for you. |
| `-u`, `--upstream` | Upstream URL. The transparent proxy uses only its origin and appends the tool's own paths, or keeps its path too with `proxy.path_mapping.mode: prefix`; the gateway posts to it as written. Without an explicit upstream, transparent mode follows supported native endpoint settings, then the provider default. |
| `--port` | Proxy port. `0` (default) picks a free one. |
| `--env-var` | Also set this environment variable to the proxy URL. Repeatable. |
| `--session-strategy` | `ephemeral`, `client`, or `conversation` (default). Overrides the config. |
| `--no-anchor` | Skip the default automatic anchor; an explicit config or environment setting still applies. See [Anchors](masking.md#anchors). |
| `--harvest` / `--no-harvest` | Also mask what the working directory tells: the authors of its last 1000 commits, the owner and name of each git remote, the values in its `.env` files, and the local OS username. Read at launch and kept in memory, never written. Default: the `harvest` setting, off. |
| `--preset` | Apply the Privyx settings of a preset under [`presets`](configuration.md#presets), above the profile's own `presets` and `privyx` settings and below other flags. Repeatable; later ones win. |
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
| `opencode` | `anthropic` | `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`, ending in `/v1`. For a model of OpenAI, add `--provider openai`. |
| `grok` | `openai`, upstream `https://api.x.ai/v1` | `GROK_XAI_API_BASE_URL`, ending in `/v1`, used when Grok Build signs in with an API key (`XAI_API_KEY`) |
| `gemini` | `google` | `GOOGLE_GEMINI_BASE_URL`, used when Gemini CLI signs in with an API key (`GEMINI_API_KEY`); also `CODE_ASSIST_ENDPOINT` and `GOOGLE_VERTEX_BASE_URL`, so the other sign-ins reach Privyx and are refused |
| `ollama` | `generic`, upstream `http://127.0.0.1:11434` or the server `OLLAMA_HOST` names | `OLLAMA_HOST`. `ollama:<model>` runs `ollama run <model>`. |

Gemini CLI goes through Privyx only when it signs in with an API key. Signed
in with Google, it calls Google's Code Assist service, and with Vertex AI
another endpoint; the `gemini` profile points both at Privyx too and sets
`proxy.passthrough_unknown: false` for its launch, so those requests are
refused with a `403` instead of reaching Google unmasked. Choose the API key
sign-in in Gemini CLI (`/auth`) to use it through Privyx.

`ollama:<model>` checks that an Ollama server answers before it starts the
tool, at `http://127.0.0.1:11434`, or at the server `OLLAMA_HOST` names when
it is set (read as Ollama reads it). `--upstream` or an `upstream` in your
`ollama` profile wins over `OLLAMA_HOST`. A profile of your own named
`ollama:<something>` is used as it is, not as a model name.

pi has no base-URL variable or flag; the extension changes its provider for
this run only, never `models.json`. Only that one provider goes through
Privyx, so pi refuses a model of any other provider at startup, puts the
previous model back when `/model` picks one, and does not send a prompt to
it. For another provider, set `PRIVYX_PI_PROVIDER` and the upstream in a
profile (`pi:deepseek` in [Coding agents](../tutorials/coding-agents.md)).
A provider whose base URL has a path of its own (groq, openrouter) needs
`proxy.path_mapping.mode: prefix`, set in the profile's `privyx:` block, and
that base URL as the upstream. Without it the transparent proxy keeps only the
upstream's origin, and those requests miss the provider's API.

In transparent mode, an explicit `--upstream`, profile `upstream`,
`upstream_url`, or `provider.base_url` wins. Otherwise, Privyx follows these
native destinations before replacing the agent's endpoint with the proxy URL:

| Agent | Native destination |
|---|---|
| Claude Code | `ANTHROPIC_BASE_URL` in the child environment; user, project, and local settings override it in that order |
| Codex | A custom provider's `base_url`; for the built-in OpenAI provider, `openai_base_url` in config or `-c` overrides. Codex does not read `OPENAI_BASE_URL` |
| OpenAI CLI | `OPENAI_BASE_URL` |
| Aider | `OPENAI_API_BASE`, then `OPENAI_BASE_URL` |
| Grok | `GROK_XAI_API_BASE_URL` (takes precedence over the built-in profile's xAI default) |
| Gemini CLI | `GOOGLE_GEMINI_BASE_URL` for API-key mode |
| Ollama | `OLLAMA_HOST` (takes precedence over the built-in profile's local default) |

The child environment includes profile `env` and `env_from`. Claude's user
settings come from `$CLAUDE_CONFIG_DIR/settings.json`, or
`~/.claude/settings.json`. Project settings come from `.claude/settings.json`
in the current directory, including when launched from a git subdirectory.
Local settings come from `.claude/settings.local.json` at the git root, or the
current directory outside git. `--setting-sources` limits which scopes are inspected.
Pi and OpenCode still need an explicit upstream for non-default providers;
Privyx does not infer a destination from Gemini's login/Vertex trap variables.

A derived destination is refused if Privyx would send its own configured API
key or nonempty configured provider headers to it: set `--upstream` explicitly
to confirm that destination. Custom header names can also carry credentials.
Its path must match the profile's `base_path`, because transparent mode forwards the
agent's original paths without translating a prefix. A mismatch fails before
launch with a hint to set `proxy.path_mapping.mode: prefix`, which keeps the
endpoint's path, or to configure `base_path` and matching `proxy.routes`.
`--dry-run` shows the native endpoint and its source, including when an
explicit upstream overrides it, with URL credentials, query, and fragment
redacted. An old base URL left in the shell is now followed too; unset it or
provide an explicit upstream if it no longer names the intended server.

Claude Code keeps only the last `--settings`: one of your own, in profile args
or after `--`, replaces Privyx's. Privyx refuses that combination when any
enabled settings layer defines `env.ANTHROPIC_BASE_URL`, including the supplied
JSON or file (`--settings X` and `--settings=X`). Move those options into
Claude's settings files, optionally under a separate `CLAUDE_CONFIG_DIR`, and
use `upstream` for the destination. Without a base URL in any settings layer,
your `--settings` remains intact and routing uses the child environment.
File-based managed settings with `env.ANTHROPIC_BASE_URL` are always refused:
they override Privyx's injection even with an explicit upstream. This includes
`managed-settings.json` and `managed-settings.d/*.json` in `/etc/claude-code`
on Linux, `/Library/Application Support/ClaudeCode` on macOS, or
`C:\Program Files\ClaudeCode` on Windows. Remote policies and OS-managed
policies are outside this local-file check; network enforcement is needed to
constrain destinations that change after launch.

Codex drops every `-c` given before a subcommand once one
follows it (`exec -c …`), which is why Privyx repeats its own after your
arguments. `openai_base_url` applies to codex's built-in `openai`
provider. When codex's config selects a `model_provider` of your own, Privyx
also points that provider's `base_url` at the proxy, for this launch only; the
provider keeps its headers and authentication. Unless you set an upstream,
Privyx forwards to the provider's own `base_url`, which must end in `/v1`.
Privyx reads `/etc/codex/config.toml`, `$CODEX_HOME/config.toml`, a `-p`
profile, and `-c` overrides; a provider set only in a project's
`.codex/config.toml` is not detected. Provider detection uses the child's
environment, including profile `env` and `env_from`; `CODEX_HOME` (or `HOME`
when `CODEX_HOME` is unset) is resolved before reading the Codex config.
If the tool exits without a single request forwarded on a masking route (a
path in `proxy.routes`), `privyx run` warns and records a
[`run.unproxied`](../observability/audit-events.md#rununproxied) audit event: a
tool that calls its provider directly is not masked. Requests that only list
models, or that took paths outside `proxy.routes`, do not count; the warning
gives how many passed through unmasked, were blocked, or failed.

With `--harvest`, the launch prints how many values of each kind it masks, never
the values. The local OS username is masked as `PERSON`, except names shorter
than three characters and the generic names `root`, `ubuntu`, `admin`, and `user`
(case-insensitive). On POSIX it comes from the effective user account, not
`USER` or `LOGNAME`; on other platforms it uses Python's `getpass.getuser()`.
An unavailable account name is skipped. This automatic collection is limited
to `run`: `scan` does not add the OS username to its output. To mask an excluded
name, add it to your own `terms`.

A `.env` value is masked when it is six characters or longer and is neither a number nor a single word (`true`, `development`); files ending in
`.example`, `.sample`, `.template`, or `.dist` are skipped. Each value is masked
as `ENV_<KEY>`. A repository named for a common word (`api`) masks that word
too: `privyx scan` lets you review the list first.

Any other command runs too. With `--provider openai`, Privyx sets the
variables most OpenAI clients read, `OPENAI_BASE_URL` and `OPENAI_API_BASE`, to
the proxy's URL ending in `/v1`; with `--provider anthropic`, it sets
`ANTHROPIC_BASE_URL` to the proxy's URL. The provider can also come from
`provider.type` in the config. For a tool that reads another variable, pass
`--env-var` instead: that variable gets the proxy's URL with no path, and no
other is set. If the tool ignores these variables, the warning that no request
reached Privyx says so when it exits.

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
absolute path, `environment`, `CLI`, `agents.NAME.privyx`, or `presets.NAME`.
Inherited fields retain their parent's source. The environment section lists
proxy URL variables plus masked custom
values and the source names of `env_from` references. Credential flags, JSON settings,
config assignments, URL user info, queries and fragments are masked in the
display; arbitrary free-text arguments are displayed as supplied. The plan
checks configuration, but does not verify that the agent sends requests
through the proxy. The same arguments and resolved settings are used when
you launch without `--dry-run`. Both paths consume the same launch plan,
including an environment snapshot; binding a free port changes only its
proxy URL. Missing environment references fail before startup.

With [presets](configuration.md#presets), the plan ends with the presets
applied and each setting they decide; a setting a later layer overrides is
left out:

```console
$ privyx run --dry-run claude:work --preset debug
...
Presets: strict [/home/alice/.privyx/config.yaml], debug [CLI]
  log_file = "/home/alice/.privyx/debug.log" [presets.debug]
  log_level = "debug" [presets.debug]
  proxy.passthrough_unknown = false [presets.strict]
```

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

In JSON, each string of up to 256 characters is also checked together with its
key, as the proxy does with tool arguments, so the value in
`{"password": "..."}` is masked as a secret. Numbers are checked alone, so a
card number is masked and `{"token_limit": 4000}` is not; a masked number
becomes its token, a string.

## `privyx unmask`

Put the original values back, from a `--map` file or a `--session`. Tokens the
mapping does not know are left as they are. Takes the same options as
`privyx mask`.

```bash
privyx unmask --map map.json -i masked.txt
```

## `privyx pipe`

Mask standard input to standard output one line at a time. Each line is
written as soon as it is read, so a live stream never waits for its end.

```bash
tail -f app.log | privyx pipe --session logs | your-tool
your-tool | privyx pipe --session logs --restore
```

| Option | Description |
|---|---|
| `--session ID` | Keep the mapping in this vault session. Without it, a new session is created and its id printed to standard error. Needs a persistent vault to be restored later. |
| `--restore` | Put the original values back instead of masking. Needs `--session`. The session is read again for each line, so restore can follow a `privyx pipe` that is still masking into the same session, and stops with an error once the session is deleted or expires. |

Lines are masked one by one, so a value split across lines, such as a PEM
private key, is not seen whole: use `privyx mask` for those.

## `privyx mcp wrap`

Run an MCP server that speaks over standard input and output, and mask what
it sends to the client. Put `privyx mcp wrap --` in front of the server's
command in the client's config:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "privyx",
      "args": ["mcp", "wrap", "--", "npx", "-y", "some-postgres-mcp-server"]
    }
  }
}
```

Or, for Claude Code: `claude mcp add postgres -- privyx mcp wrap -- npx -y
some-postgres-mcp-server`.

Tool results, notifications, and requests from the server are masked, so the
model and the client only see tokens. That covers object keys and numbers (a
masked number becomes its token, a string), and a value is also checked
together with its key, so the value in `{"password": "..."}` is masked as a
secret. Base64 payloads, an image's or audio's `data` and a resource's `blob`,
are passed on unread, as the proxy does. Tokens in what the client sends, such
as tool arguments, are restored first, so the server works on the real values.
The server needs no change. Its standard error is passed through as it is.

| Option | Description |
|---|---|
| `--session ID` | Keep the mapping in this vault session. Without it, a new session is created and its id printed to standard error. Any vault works, the default `memory` one included, since both directions run in one process. Stops with an error once the session is deleted or expires, so a token the client still holds is never restored from a new session. |

A message that cannot be masked is never sent: the server is stopped and
`privyx mcp wrap` exits with an error. That includes an object whose keys
masking would make the same, as the `redact` operator does with two emails.
Otherwise it exits with the server's exit code. When it stops the server, after
a masking error or on SIGTERM, it sends SIGTERM to the server and every process
the server started (such as the server `npx` runs), then SIGKILL to those still
running a second later.

Behind `privyx run` or the proxy, tool results are masked on their way to the
model already. `privyx mcp wrap` is for clients that cannot use the proxy, such
as Claude Desktop, and keeps the values out of the client's own transcripts
too. To use both, give `privyx mcp wrap` a config with its own
`token.namespace` (for example `MCP`), or an [anchor](masking.md#anchors), so
the proxy never restores one of its tokens from the proxy's own session.

## `privyx mcp serve`

Run a local MCP server over standard input and output. Add it to the client's
config:

```json
{
  "mcpServers": {
    "privyx": {
      "command": "privyx",
      "args": ["mcp", "serve"]
    }
  }
}
```

Options after `serve`: `--config FILE` selects a configuration, and
`--root DIR` the directory the file tool may use (default: the directory the
client starts the server in, which for a desktop client may not be your
project, so set it).

| Tool | Description |
|---|---|
| `privyx_scan` | Detect sensitive spans in `text` with the configured detector and policy, optionally limited to `categories` (entity types such as `EMAIL`, case-insensitive). Returns offsets (in Unicode code points) and entity types, without the detected values. With `categories`, `filtered_out` counts the findings of other types. |
| `privyx_sanitize_file` | Mask a regular UTF-8 file at `path` using the configured detector, policy, and operator. Returns the masked text, or writes it to a new `output_path`. |
| `privyx_precommit_check` | Check added lines in `git_diff`, a unified diff (a merge's combined diff and colored output too), for credentials and placeholders. Added lines no file header names are reported under `(unknown)`. Reports findings but does not install a hook or block a commit. |

`path` and `output_path` are resolved against the root, and a path outside it,
symbolic links followed, is refused: the tool can only read what is under the
root, and what it returns is masked but still the file's content. For file
sanitization, `format` can be `auto`, `text`, `json`, or `jsonl`. Existing
output files are never overwritten. JSON masking follows the same string-leaf
rules as `privyx mask`. With a persistent vault, the result includes a
`session_id` for `privyx unmask --session` with the same configuration. With
the default memory vault, the mapping is dropped after the call and the result
includes a warning instead. Review the output before sharing it: detection and
policy configuration determine what is masked.

The server also offers two prompts, which a client lists as commands. With the
server named `privyx` as above, Claude Code lists them as `/privyx:status` and
`/privyx:audit-tail`, and `/mcp__privyx__status` and `/mcp__privyx__audit-tail`
run them too. A prompt's text goes into the conversation and on to the model,
so both show what `privyx session list` and `privyx audit tail` show: ids,
times, counts, and entity types, never the masked values. A session id goes as
it is, so one a client chose with an `x-privyx-session` header reaches the
model too.

| Prompt | Description |
|---|---|
| `status` | The vault's sessions (how many, their mappings, the five most recently active) and the audit trail's totals: events, requests, errors, masked entities by type, and restored pseudonyms. |
| `audit-tail` | The 10 latest audit events, one line each, as `privyx audit tail` prints them. |

With the default memory vault each privyx process keeps its own sessions, so
`status` lists none. With a persistent vault, start the server with the
proxy's `--config` to see the proxy's sessions. Neither prompt takes
arguments, and none masks or restores text: a client may send a command's
arguments to the model along with the prompt's result, as Claude Code does, and
a restored text would reach the model too. Use `privyx mask` and
`privyx unmask --session` in a terminal for that.

A failed call names the problem (file not found, output exists, path outside
the root, ...). When the cause could quote file content, the client is only
told that the call failed, and the server logs the error type to standard
error. Configuration is loaded once, when the server starts: a broken config
stops it there with an error.

## `privyx session`

List, show, and delete vault sessions. These need a `sqlite` or `redis` vault;
see [Sessions and vault](sessions.md).

```bash
privyx session list
privyx session show SESSION_ID [--reveal]
privyx session prune --older-than 7d [--dry-run]
privyx session export SESSION_ID --forensic [-o FILE]
```

| Command | Option | Description |
|---|---|---|
| `list` | | Sessions, most recently active first, with mapping counts. Never shows values. |
| `show` | | The session's mappings, then its responses, tokens, prompt cache hit rate, and estimated cost, read from the audit trail. A session that is not in the vault shows only the latter. |
| `show` | `--reveal` | Print original values in full. Without it they are masked. |
| `prune` | `--older-than` | Required. Delete sessions idle at least this long: `30m`, `12h`, `7d`, `2w`. |
| `prune` | `--dry-run` | List what would be deleted and delete nothing. |
| `export` | `--forensic` | Required. Write the session's audit events, its masked transcript from the [tap](../observability/tap.md), and a summary to a zip archive, without original values. |
| `export` | `-o`, `--output` | Archive path. Default `SESSION_ID-forensic.zip`; an existing file is never overwritten. |

The cost is an estimate from built-in prices for Claude models; add other
models, or correct a price, with [`audit.prices`](configuration.md#audit).

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
privyx audit watch [SESSION_ID] [-n 20] [--no-follow]
```

| Command | Option | Description |
|---|---|---|
| `stats` | `--since` | Only count events from the last `30m`, `12h`, `7d`, `2w`. |
| `tail` | `-n`, `--lines` | Recent events to show first. Default `10`. |
| `tail` | `--follow` / `--no-follow` | Keep printing new events (default) or stop. |
| `watch` | | What masking changed in each exchange, from the [tap](../observability/tap.md), as a word diff. Needs `audit.tap: true`. |
| `watch` | `-n`, `--lines` | Recent records to show first. Default `10`. |
| `watch` | `--follow` / `--no-follow` | Keep showing new records (default) or stop. |

See [Audit events](../observability/audit-events.md) for what each event holds.

## `privyx config`

Show the configuration.

```bash
privyx config            # a summary
privyx config --show     # every setting as JSON, credentials masked
privyx config --sources  # each setting's masked value and winning source as JSON
privyx config --path     # the config files in use, or "defaults"
privyx config --schema   # the JSON Schema of a config file
```

`--path` lists files without validating setting values or contacting password
managers. Included files appear before the file that includes them. File and
include errors are still reported.

`--sources` maps dotted field paths to `value` and `source`, for example
`"port": {"value": 9000, "source": "environment"}`. Sources are `defaults`,
absolute file paths (including included files), or `environment`. The source
identifies the layer that supplied the setting; a `${VAR}` substitution inside
a file still belongs to that file. Fields filled by the settings schema are
reported as `defaults`. Detector lists use zero-based paths such as
`detector.0.type`; lists of scalar values stay together. Credentials, header
values, and detector rules use the same masking as `--show`.

Unlike `--path`, `--sources` loads and validates settings and resolves secret
references outside agent profiles' `env`, `args`, and `privyx`. When inspection flags are combined, the order is `--schema`,
`--sources`, `--show`, then `--path`.

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
- `.env` files: each key whose value is worth masking (as in `privyx run --harvest`) and is not already masked by the built-in detector, as a pattern `ENV_<KEY>` that masks the value assigned to that key (`KEY=…`, `KEY: …`, `"KEY": "…"`). The values themselves are never written.

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

## `privyx install-hook`

Install git hooks that stop a commit holding a credential or a Privyx
placeholder such as `<PRIVYX_EMAIL_1>`.

```bash
privyx install-hook
```

It writes `pre-commit` and `commit-msg` in the repository's hooks directory
(`core.hooksPath` when set). Each runs `privyx hook`, so `privyx` must be on the
`PATH` of whatever runs `git commit`. A hook of another tool is left alone: the
command exits with an error naming the line to add to it. Running it again
rewrites only its own hooks. To remove them, delete the two files.

A blocked commit lists each finding by file and line and the kind of value,
never a secret itself:

```console
$ git commit -m "Add the client"
privyx: this commit holds values that must not leave this machine:
  src/client.py:12  API_KEY
  commit message:1  placeholder <PRIVYX_EMAIL_1>
Remove the secrets, restore placeholders with `privyx unmask`, or commit with --no-verify to skip this check.
```

`git commit --no-verify` skips both hooks. Aider skips them unless it runs with
`--git-commit-verify`; give it that option to keep a placeholder out of the
messages it writes.

## `privyx hook`

The checks the installed hooks run. They exit with status 1 when they find
something.

```bash
privyx hook pre-commit             # the lines the staged changes add
privyx hook commit-msg FILE        # a commit message file, # lines included
```

Both look for the built-in credential types (`API_KEY`, `PRIVATE_KEY`, `JWT`,
`AUTH_TOKEN`, `URL_CREDENTIAL`, `SECRET`) and for placeholders in the configured
`token` format. Personal data such as an email address is not a reason to
refuse a commit: code and tests are full of it.

`pre-commit` reads the staged content itself, as text: not the view a
`textconv` diff driver gives of it, and a file marked binary (`-diff` in
`.gitattributes`) too. `commit-msg` checks the whole message file, `#` lines
included: `git commit -m` keeps them, and a hook cannot tell which cleanup mode
a commit uses. With `git commit -v`, that includes the diff shown below the
scissors line, so a placeholder the commit removes is reported too; commit
with `--no-verify` then.

## `privyx doctor`

Build every configured component and push a request through it: plugins,
detector, vault (a real write and read), provider, proxy, and streaming
restore. The `routes` check lists masking paths, gaps in the default route map,
and the disposition of unlisted paths. Passthrough prints a warning; invalid
routes fail. Exits with a non-zero status if any check fails.

Configuration is loaded once per invocation, including password-manager
references, and all checks use the same settings. A later invocation reads
configuration again. If loading fails, doctor reports a single configuration
failure and stops before running component checks.

Doctor tries a sample email. When every detector is a built-in `regex` or `yaml`
detector, it also tries the first nonempty term of each configured entity. With
any other detector, including LLM and plugin detectors, it uses only the
synthetic email so configured terms are not sent to those detectors. The
detector check scans without the policy; the proxy and streaming checks use
only samples the configured detector and policy select for masking.
If no sample applies, the check explicitly reports `skipped`. This does not
verify coverage for arbitrary patterns or other input. Sample values are not
printed. Streaming restore is also skipped for an irreversible operator.

Credentials, query strings, and fragments in the displayed upstream URL are
masked. Doctor deletes its test sessions after each check, including failures,
and leaves existing sessions untouched. A cleanup failure is a failed check;
the vault connection is still closed.

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
