---
description: Run Claude Code, Codex, or aider through Privyx so that the secrets and personal data they read from your project are masked before they reach the provider.
---

# Coding agents

A coding agent sends far more than your prompt. Every file it reads, every
command output, and every log line becomes part of a request to the provider:
your `.env`, a stack trace with a customer's email address, the URL of an
internal git remote. This tutorial runs the agent through Privyx, so those
values are masked on the way out and restored on the way back.

**You need:** Privyx [installed](../guide/installation.md), and Claude Code,
Codex, or aider working on its own. **Time:** ten minutes.

## 1. Start the tool through Privyx

```bash
privyx run claude
```

```console
$ privyx run claude
Privyx proxy → https://api.anthropic.com
Running: claude --settings '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:38383"}}'
  ANTHROPIC_BASE_URL = http://127.0.0.1:38383
```

Claude Code then starts as it always does. `privyx run` did three things
first:

1. It started a proxy on a free port on `127.0.0.1`.
2. It launched the tool with its base URL pointed at that proxy.
3. It will stop the proxy when the tool exits, and exit with the tool's own
   exit code.

The tool keeps its own login or API key; Privyx relays it to the provider.
Arguments after `--` go to the tool:

```bash
privyx run claude -- --continue
```

The other tools it knows:

=== "Codex"

    ```bash
    privyx run codex
    ```

    ```console
    $ privyx run codex
    Privyx proxy → https://api.openai.com
    Running: codex -c 'openai_base_url="http://127.0.0.1:43407/v1"'
      OPENAI_BASE_URL = http://127.0.0.1:43407/v1
    ```

    This covers Codex's built-in `openai` provider. With a `model_provider`
    of your own in Codex's config, Codex does not go through Privyx; see
    [Check that it works](#3-check-that-it-works).

=== "aider"

    ```bash
    privyx run aider
    ```

    Privyx sets `OPENAI_API_BASE` and `OPENAI_BASE_URL` for it, both ending
    in `/v1`.

=== "Another tool"

    Any tool that reads its base URL from an environment variable works.
    Name the variable and the provider:

    ```bash
    privyx run -u https://api.example.com --env-var MY_TOOL_BASE_URL -- my-tool --flag
    ```

    The variable gets the proxy's URL without a path. `privyx run --list`
    shows the tools Privyx knows by name.

## 2. Work as usual

Ask the agent to do something that touches sensitive text, for example:

> Read `.env` and tell me which variables are set.

On your screen, nothing looks different: the answer names your real values.
The provider received something else. With this `.env`:

```bash
DB_PASSWORD=hunter2
SMTP_USER=dana@acme.example
```

the file reached the provider as:

```text
DB_PASSWORD=<PRIVYX_SECRET_26EA7D0A8B8DC4C2>
SMTP_USER=<PRIVYX_EMAIL_4CE2AF0BB2282EF4>
```

The same happens to tool arguments in the other direction. When the model
decides to write `<PRIVYX_EMAIL_4CE2AF0BB2282EF4>` into a file, the tool call
your machine receives holds `dana@acme.example`.

!!! note "Why these tokens are longer"

    `privyx run` creates an anchor secret in `~/.privyx/anchor.key`
    the first time it runs, so a token's id is derived from the value instead
    of counted. The same value gets the same token in every turn and after a
    restart, which keeps a long conversation, and the provider's prompt
    cache, consistent. See [Anchors](../guide/masking.md#anchors).

## 3. Check that it works

When you leave the tool, look at the audit trail. Privyx keeps it in
`~/.privyx/`, not in the project:

```console
$ privyx audit stats
Audit log:  /home/dana/.privyx/audit.log
Period:     2026-10-02T14:34:27 → 2026-10-02T14:34:28
Events:     13
Requests:   3
Responses:  3 (avg 0.04s)
Errors:     0
Sessions:   1 created, 0 deleted
Masked:     117 entities
Restored:   26 pseudonyms

Masked by entity type:
  ORGANIZATION  55
  EMAIL         31
  PERSON        27
  IP_ADDRESS    4
```

The trail holds counts and entity types, never the values. `--since 1h`
limits it to the last hour, and `privyx audit tail FILE` follows it live from
a second terminal.

Two things to look for:

- **Requests above zero.** If the tool exits without having sent a single
  request through the proxy, `privyx run` says so:

    ```text
    Warning: no request from claude reached Privyx; if it called its provider, it did so directly, without masking.
    ```

    That happens when the tool's own configuration outranks what `privyx run`
    sets: a `model_provider` of your own in Codex, or a `--settings` of your
    own after `--` for Claude Code. If your Claude Code settings point
    `ANTHROPIC_BASE_URL` at a router or another proxy, give that URL to
    Privyx instead, as `privyx run -u URL claude`: Privyx then sits in front
    of it.

- **The entity types you expect.** Emails, keys, and IP addresses show up
  without configuration. Names do not; that is the next step.

## 4. Add your own names and terms

The built-in patterns recognize values by their shape. Your name, your
employer, and your project's codename have no shape, so Privyx cannot know
they matter until you list them. They are also what a coding agent sends
most often: they are in your home directory's path, your git branch names,
and your commit log.

Put them in your config file, which Privyx reads on every run:

```yaml
# ~/.privyx/config.yaml
detector:
  type: regex                 # keeps the built-in patterns
  terms:
    PERSON:       [dana, dana whitfield, dwhitfield]
    ORGANIZATION: [acme]
    PROJECT:      [bluebird]
```

Try it before you rely on it:

```console
$ privyx detect --transform "/home/dwhitfield/acme/bluebird on branch feature/dana-login"
     6:16    PERSON            'dwhitfield'
    17:21    ORGANIZATION      'acme'
    22:30    PROJECT           'bluebird'
    49:53    PERSON            'dana'

/home/<PRIVYX_PERSON_1>/<PRIVYX_ORGANIZATION_2>/<PRIVYX_PROJECT_3> on branch feature/<PRIVYX_PERSON_4>-login
```

Every run uses it from now on:

```bash
privyx run claude
```

A file kept elsewhere is named with `-c`, or with `PRIVYX_CONFIG` in your
shell profile, and is then read in place of this one. A project can add
settings of its own in a `privyx.yaml`, which Privyx reads once you have run
`privyx trust` in that directory; see
[Configuration](../guide/configuration.md#where-settings-come-from).

Matching ignores case and only hits whole words, so `dana` does not match
inside `danasaur`. [Your own names and terms](custom-terms.md) goes further:
patterns for IDs, and detectors that find names without a list.

## 5. Share one proxy between terminals

`privyx run` starts a proxy per tool. To run one proxy for several terminals
or tools instead, start it yourself and point each tool at it:

```bash
export PRIVYX_SESSION_STRATEGY=conversation
export PRIVYX_ANCHOR_SECRET=$(openssl rand -hex 32)   # keep it; reuse it on every start
privyx proxy --upstream https://api.anthropic.com
```

```bash
ANTHROPIC_BASE_URL=http://localhost:8000 claude
```

The two variables give this proxy what `privyx run` sets up by itself: one
session per conversation and stable tokens. A base URL in Claude Code's own
`settings.json` outranks the environment variable; `privyx run` handles that
for you, a shared proxy does not.

## What Privyx covers for these tools

- **Masked:** the system prompt, your messages, file contents and command
  output the agent sends back as tool results, tool arguments, and the
  descriptions of tools (an MCP server writes those). Also what the tool
  reports about your machine: Claude Code's working directory, home, user
  name, git branch, and remotes, and Codex's workspace paths and remote URLs.
  As everywhere, a value is masked only when a detector finds it, so list
  your user name and organization as shown above.
- **Restored:** the model's answers, its reasoning, and the arguments of the
  tool calls it makes.
- **Not masked:** requests to paths other than the chat endpoints, which are
  forwarded as the tool sent them; images and other binary content; and
  anything a tool sends to a host other than its provider's API.
  [Limitations](../security/limitations.md) has the details.

## Next steps

- [Your own names and terms](custom-terms.md): word lists, patterns, Presidio,
  and the LLM detector.
- [Sessions and vault](../guide/sessions.md): how sessions are chosen and where
  they are stored.
- [Troubleshooting](../guide/troubleshooting.md): a `503` from Privyx, a token
  left in an answer, and other symptoms.
- [`privyx run` reference](../guide/cli.md#privyx-run): every option, and how
  each known tool is pointed at the proxy.
