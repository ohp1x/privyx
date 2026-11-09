---
description: Why Cursor cannot use a Privyx proxy on localhost, and what a reachable gateway does and does not protect.
---

# Cursor

Cursor accepts your own key and an **Override OpenAI Base URL** in its model
settings, but the requests to that URL are sent from Cursor's servers, not from
your machine. `http://localhost:8000` is therefore unreachable for them.

Two consequences:

- Privyx must be reachable from the internet, with TLS and access control, as
  in [A shared gateway for a team](../tutorials/team-gateway.md). Set the
  override to that gateway's URL ending in `/v1`.
- Your code and prompts reach Cursor's servers before they reach Privyx.
  Privyx then masks what goes on to the model provider, but not what Cursor
  itself receives.

If the goal is that no sensitive value leaves your machine, use an agent that
calls the provider from your machine, such as one in
[Coding agents](../tutorials/coding-agents.md), or an editor extension like
[Cline](cline.md), [Roo Code](roo-code.md), or [Continue](continue.md).
