"""Codex (``codex exec``).  It has no scripted upstream: it runs with ``--upstream``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import PROMPT, Agent


def setup(root: Path, server: dict[str, Any], transparent: bool) -> dict[str, str]:
    home = root / "home" / ".codex"
    home.mkdir(parents=True)
    # A JSON string or list of strings is also valid TOML.
    toml = "".join(f"{k} = {json.dumps(v)}\n" for k, v in server.items())
    (home / "config.toml").write_text(f"[mcp_servers.canary]\n{toml}")
    return {"CODEX_HOME": str(home)}


# Codex sends its instructions and tools as ``input`` items for some models, so
# no key is checked for stability yet.
AGENT = Agent(
    creds=("CODEX_API_KEY",),
    endpoint="/v1/responses",
    stable=(),
    setup=setup,
    args=lambda _: ["exec", PROMPT],
    point=lambda url: ({}, ["-c", f"openai_base_url={json.dumps(url + '/v1')}"]),
    main=lambda r: r["body"] is not None and r["path"].startswith("/v1/responses"),
)
