---
description: Run Claude Code, Codex, aider, or pi through Privyx so that the secrets and personal data they read from your project are masked before they reach the provider.
---

# Coding agents

A coding agent sends far more than your prompt. Every file it reads, every
command output, and every log line becomes part of a request to the provider:
your `.env`, a stack trace with a customer's email address, the URL of an
internal git remote. This tutorial runs the agent through Privyx, so those
values are masked on the way out and restored on the way back.

**You need:** Privyx [installed](../guide/installation.md), and Claude Code,
Codex, aider, or pi working on its own. **Time:** ten minutes.

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

    This covers Codex's built-in `openai` provider and a `model_provider` of
    your own, such as a company router: Privyx points that provider at the
    proxy for this launch and forwards to the provider's own `base_url`. Your
    Codex config files are unchanged.

=== "aider"

    ```bash
    privyx run aider
    ```

    Privyx sets `OPENAI_API_BASE` and `OPENAI_BASE_URL` for it, both ending
    in `/v1`.

=== "pi"

    ```bash
    privyx run pi
    ```

    pi reads no base-URL variable, so Privyx loads a small extension
    (`pi -e`) that points pi's providers at the proxy for this run. Your
    `models.json` is not changed. Each request still goes to the provider of
    the model, with pi's own key, after Privyx has masked it, and so does a
    model you switch to with `/model`:

    ```bash
    privyx run pi -- --model deepseek/deepseek-flash
    ```

    pi refuses a model whose requests Privyx cannot mask, such as one on
    Amazon Bedrock or signed in with GitHub Copilot: at startup, where it
    says so once its screen has closed and exits with status 1, and when you
    switch to it with `/model`.

    To send pi's requests to one upstream, such as a router, name the pi
    provider and the upstream in a profile:

    ```yaml
    agents:
      pi:deepseek:
        provider: openai
        upstream: https://api.deepseek.com
        env: {PRIVYX_PI_PROVIDER: deepseek}
    ```

    ```bash
    privyx run pi:deepseek -- --model deepseek/deepseek-flash
    ```

    pi then refuses the models of every other provider.

=== "Another tool"

    Any tool that reads its base URL from an environment variable works.
    Name the variable and the provider:

    ```bash
    privyx run -u https://api.example.com --env-var MY_TOOL_BASE_URL -- my-tool --flag
    ```

    The variable gets the proxy's URL without a path. `privyx run --list`
    shows the tools Privyx knows by name: besides those above, `openai`
    (OpenAI CLI), `opencode`, `gemini` (Gemini CLI), `grok` (Grok Build), and
    `ollama`; see [Integrations](../integrations/index.md#coding-tools).

### Save a launch profile

For a tool you launch the same way each time, save a named profile in
`~/.privyx/config.yaml` and start it with `privyx run NAME`.
[Agent profiles and presets](../guide/agent-profiles.md) shows how, including
Privyx settings such as a debug log that you switch on for one launch.

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
DB_PASSWORD=<PRIVYX_SECRET_A2907293658B4995>
SMTP_USER=<PRIVYX_EMAIL_19ECE4788CA5AE6F>
```

The same happens to tool arguments in the other direction. When the model
decides to write `<PRIVYX_EMAIL_19ECE4788CA5AE6F>` into a file, the tool call
your machine receives holds `dana@acme.example`.

!!! note "Where the token ids come from"

    Privyx creates an anchor key in `~/.privyx/anchor.key` the first time it
    runs, and derives a token's id from the value with it instead of counting. The same value gets the same token in every turn and after a
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

- **Requests on a masking route.** After you sent the tool a prompt,
  `Requests` counts it. If it does not, the prompt did not go through Privyx,
  and `privyx audit tail` shows a `run.unproxied` event for that launch.
  `privyx run` warns on the terminal only when a request passed through
  unmasked with a body, or was blocked or failed: no request at all is also
  what `--help`, or a tool closed before its first prompt, looks like.

    A prompt that misses Privyx usually means the tool's own configuration
    outranks what `privyx run` sets, such as a `model_provider` set only in a
    project's `.codex/config.toml`.
    Supported native endpoints, including Claude's `ANTHROPIC_BASE_URL`
    settings, are followed automatically unless you set an explicit upstream.
    Claude settings that would override proxy routing are rejected before
    launch. Check `run --dry-run` and the [routing rules](../guide/cli.md#privyx-run).

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
detectors:
  regex:                      # keeps the built-in patterns
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

/home/<PRIVYX_PERSON_ECC822471C9C891F>/<PRIVYX_ORGANIZATION_FDF5485CE41A6AAA>/<PRIVYX_PROJECT_DBBA12EA1BBFFECF> on branch feature/<PRIVYX_PERSON_CC59DAE7AAAA2ED5>-login
```

Every run uses it from now on:

```bash
privyx run claude
```

A file kept elsewhere is named with `-c`, or with `PRIVYX_CONFIG` in your
shell profile, and is then read in place of this one. A project can add
settings of its own in a `privyx.yaml`, which Privyx reads once you have run
`privyx config trust` in that directory; see
[Configuration](../guide/configuration.md#where-settings-come-from).

Matching ignores case and only hits whole words, so `dana` does not match
inside `danasaur`. [Your own names and terms](custom-terms.md) goes further:
patterns for IDs, and detectors that find names without a list.

In a project, [`privyx scan`](../guide/cli.md#privyx-scan) collects such names
for you: git authors, the owner and name of each remote, the project name, and
the values of `.env` keys. It asks what to leave out and adds the rest to the
project's config. [`privyx config lint`](../guide/cli.md#privyx-config-lint) then lists the
spellings of your terms that still get through, such as `DB_DANA` or
`dana_dev`, with the option that masks each.

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

## 6. Tell the agent about placeholders

A model that has never seen a Privyx placeholder may take one for a typo or a
template variable and "fix" it, or try to work out the value behind it. Paste
these rules where your agent reads its instructions:

```markdown
## Privyx placeholders

Requests may go through Privyx, which replaces personal data and secrets with
placeholders such as `<PRIVYX_EMAIL_4FF5EF825E496E1A>` before they reach you, and puts the
real values back into your replies and tool calls.

- Treat a placeholder as the value itself. Copy it exactly as written,
  brackets included, wherever that value is needed: replies, code, commands,
  file contents, and tool calls.
- A placeholder is not a typo or a template variable. Do not fix it, split
  it, or replace it with an example value.
- Do not guess, decode, or reconstruct the value behind a placeholder, and do
  not ask for it.
- Do not make up placeholders. Only the ones you were given are restored; an
  invented one stays in the output as written.
- When code, tests, or docs only need an example value, write an obviously
  fake one (`ana@example.com`, `+15550100`), not a placeholder: a placeholder
  written into a file becomes the real value there.
```

| Agent | For every project | For one project |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` | `AGENTS.md` |
| Cursor | User Rules, in Cursor's settings | `AGENTS.md` |

Other agents have a file of their own for project instructions; the rules go
there. If you changed `token.namespace`, `token.format`, or `token.compact`,
change the example placeholder to match. The
[placeholder directive](../guide/masking.md#placeholder-directive) sends a
short form of the first three rules with every request, which covers any
client without a file; the two work together. The last rule matters with every operator: a
`faker` or `fpe` substitute is restored in a file just as a placeholder is.

If a placeholder lands in a file anyway,
[`privyx hook install`](../guide/cli.md#privyx-hook-install) stops the commit
that holds it, and a commit holding a secret too.

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
- **Not masked:** requests to paths outside the
  [masking routes](../guide/proxy.md#routes), which are forwarded as the tool
  sent them; images and other binary content; and
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
