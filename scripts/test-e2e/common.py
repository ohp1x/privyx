"""What the end-to-end run and its agents share: the canaries and ``Agent``."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PERSON = "Zorblatt Quandry"
GIT_USER = "Quinta Vexley"  # Claude Code reports it in gitStatus ("Git user: ...")
ORG = "Canarycorp"
EMAIL = "zorblatt.quandry@canary.test"
IP = "10.20.30.40"
CANARIES = (PERSON, GIT_USER, ORG, EMAIL, IP)
TOKEN = re.compile(r"<PRIVYX_[^<>\s]+>")
PROMPT = (
    f"Look up {PERSON} at {ORG} ({EMAIL}) with the lookup tool, read notes.txt, then "
    "answer with one line: Echo: <name>, <organization>, <email>, <IP address>"
)
DUMMY_KEY = "sk-ant-e2e-dummy"  # the credential of a run against a scripted upstream

Record = dict[str, Any]  # a forwarded request: its path, headers, and JSON body (or None)


@dataclass(frozen=True)
class Agent:
    """One agent CLI: how to configure it, launch it, and read its requests.

    Attributes:
        creds: Environment variables that may hold its credential; the first
            gets the dummy key when the upstream is scripted.
        endpoint: The path Privyx masks for it.
        stable: Body keys that must not change between turns (prompt cache).
        setup: Writes its config under ``root / "home"`` — the canary MCP server
            included, and whatever a run that is not ``transparent`` has to
            overcome — and returns the environment it needs.
        args: A headless run of ``PROMPT``.
        point: The environment and arguments that aim it at a proxy URL.
        main: Whether a record is a turn of its agent loop.
        fake: The scripted upstream's reply to a path and body, as payload and
            content type.  Without one the agent needs a real upstream.
        checks: Results only a scripted run can give, as ``(ok, what, detail)``.
    """

    creds: tuple[str, ...]
    endpoint: str
    stable: tuple[str, ...]
    setup: Callable[[Path, dict[str, Any], bool], dict[str, str]]
    args: Callable[[Path], list[str]]
    point: Callable[[str], tuple[dict[str, str], list[str]]]
    main: Callable[[Record], bool]
    fake: Callable[[str, dict[str, Any]], tuple[bytes, str]] | None = None
    checks: Callable[[list[Record]], Iterator[tuple[bool, str, str]]] | None = None
