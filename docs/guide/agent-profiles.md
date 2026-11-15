---
description: Save how you launch a coding agent as a named profile, build on a built-in profile, and switch Privyx settings on for some launches with presets.
---

# Agent profiles and presets

`privyx run claude` launches Claude Code from a built-in profile. Write a
profile of your own for a tool you launch the same way each time, an endpoint
such as a company router, or Privyx settings you want for some launches only.
If you have not used `privyx run` yet, start with the
[Coding agents](../tutorials/coding-agents.md) tutorial. Every profile field
is listed under [`agents`](configuration.md#agents).

## Save a launch profile

For a tool you run with the same settings each time, add a named profile to
`~/.privyx/config.yaml`:

```yaml
agents:
  claude-work:
    command: claude
    provider: anthropic
    upstream: https://router.example.com
    base_path: ""
    env_vars: [ANTHROPIC_BASE_URL]
```

Inspect it, then launch it:

```bash
privyx run --dry-run claude-work
privyx run claude-work
```

The plan shows the command, routing, injected URL variables, and which
configuration layer supplied each setting. It creates no files and starts
no process. A project's `privyx.yaml` can override a profile after
`privyx trust`; environment and CLI overrides still win.

## Build on a built-in profile

To keep the built-in profile's routing settings and add launch defaults, use inheritance:

```yaml
agents:
  claude-work:
    extends: claude
    args: ["--verbose"]
    env:
      MY_CUSTOM_FLAG: "enabled"
    env_from:
      ANTHROPIC_AUTH_TOKEN: WORK_ANTHROPIC_TOKEN
```

Set `WORK_ANTHROPIC_TOKEN` in your shell first. Privyx reads its value only
when launching or inspecting this profile, and masks it in the preview.
Arguments after `--` follow the profile's defaults. A wrapper can override
`command` while keeping the inherited Claude adapter.

## Reuse settings with presets

Give each endpoint a profile of its own, and keep the Privyx settings you
switch on and off in [presets](configuration.md#presets), so no
profile is needed per combination:

```yaml
presets:
  strict:
    proxy:
      passthrough_unknown: false
  debug:
    log_level: debug
    log_file: "${HOME}/.privyx/debug.log"

agents:
  claude:work:
    upstream: https://router.example.com
    env_from:
      ANTHROPIC_AUTH_TOKEN: WORK_ANTHROPIC_TOKEN
    presets: [strict]
  claude:work-backup:
    extends: claude:work
    upstream: https://backup.example.com
```

```bash
privyx run claude:work                     # strict
privyx run claude:work-backup              # strict, inherited
privyx run claude:work --preset debug      # strict and debug, this launch only
```

`strict` answers paths outside [`proxy.routes`](proxy.md#routes)
with `403` instead of forwarding them unmasked. It covers only requests that
reach Privyx: a tool, extension, or subprocess that connects to its provider
directly is not stopped. Some agents call support paths that are not in the
routes, and those then fail; [`--dry-run`](cli.md#privyx-run) and
the warning when the agent exits help find them.

`debug` writes a log file, because `privyx run` leaves the terminal to the
agent and prints no log lines. At `debug` level that file also holds the
vault's database rows, which include the original values Privyx masked, when
the vault is SQLite. Keep it private (Privyx creates it with mode `0600`),
turn the preset off when you are done, and delete the file.

## See also

- [`agents`](configuration.md#agents): every profile field, budgets, and
  settings per profile.
- [Presets](configuration.md#presets): what a preset may set, and the order in
  which layers apply.
- [`privyx run`](cli.md#privyx-run): the known targets, where requests go, and
  the launch plan.
