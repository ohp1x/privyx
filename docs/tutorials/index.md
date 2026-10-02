---
description: Step-by-step Privyx tutorials for coding agents, applications, custom detection, file pipelines, a shared team gateway, and detector plugins.
---

# Tutorials

Each tutorial takes one use case from nothing to a working setup, with the
commands to run and the output to expect.

If you have not installed Privyx yet, start with the
[Quickstart](../guide/getting-started.md).

<div class="grid cards" markdown>

-   **[Coding agents](coding-agents.md)**

    ---

    Run Claude Code, Codex, or aider through Privyx, check what was masked,
    and add your own names and project terms.

    *10 minutes*

-   **[An app on the OpenAI or Anthropic SDK](sdk-app.md)**

    ---

    Chat, streaming, tool calls, and the Responses API from Python and Node,
    with one session per user.

    *15 minutes*

-   **[Your own names and terms](custom-terms.md)**

    ---

    Word lists and patterns for people, companies, codenames, and IDs, then
    Presidio or an LLM for names you cannot list.

    *15 minutes*

-   **[Files and logs in a pipeline](mask-files.md)**

    ---

    Mask a log, a JSON document, or a JSONL dataset before it leaves, and
    restore the answer. No proxy involved.

    *10 minutes*

-   **[A shared gateway for a team](team-gateway.md)**

    ---

    Docker Compose with a Redis vault, a gateway token checked by nginx, TLS,
    and an audit trail.

    *30 minutes*

-   **[A detector plugin](detector-plugin.md)**

    ---

    Detection that needs code: a worked example that finds IBANs by their
    checksum, with options and a test.

    *20 minutes*

</div>

## Where to go after a tutorial

- The [guides](../guide/detection.md) explain one topic each in depth:
  detection, masking, sessions, the proxy, and deployment.
- [Integrations](../integrations/index.md) has a page per provider and
  framework.
- The reference pages list every [setting](../guide/configuration.md),
  [command](../guide/cli.md), and [error](../reference/errors.md).
