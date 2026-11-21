"""The agent kind: how a tool is pointed at the local proxy, and what it sends.

A profile under ``agents`` names its tool's ``type``.  The type knows the
tool: where it would send requests on its own, the arguments and variables
that point it at Privyx instead, and the request keys and headers in which
its client describes the machine it runs on.  What every launch keeps is the
core's (:mod:`privyx.agents._plan`): the proxy URL in ``env_vars``, Privyx's
own credentials kept from the tool's endpoint, and argv without a shell.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from privyx.core import Component


@dataclass(frozen=True)
class Endpoint:
    """Where a tool would send its requests on its own, and where that was read."""

    url: str
    #: Where it was read, shown by ``privyx run --dry-run``: ``environment OPENAI_BASE_URL``.
    origin: str
    #: The tool's own provider that names it, such as Codex's ``model_providers.<id>``.
    provider: str | None = None


@dataclass(frozen=True)
class LaunchContext:
    """What a type knows of one launch before the proxy URL is injected."""

    command: str
    #: The profile's arguments, then the user's.
    argv: tuple[str, ...]
    #: The tool's environment before the proxy URL is injected, the profile's ``env`` included.
    environment: Mapping[str, str]
    #: The provider type in effect.
    provider: str
    #: Whether each request names its own upstream (:attr:`Agent.upstream_per_request`).
    per_request: bool = False


class Agent(Component, kind="agent"):
    """A tool ``privyx run`` launches: how it is pointed at the proxy.

    Every method has a default that does nothing, so a type overrides only
    what its tool needs.
    """

    #: Top-level request keys this tool's client fills with machine metadata.
    #: The union of every registered type is masked in every request: the
    #: proxy cannot tell which tool sent one.
    metadata_keys: ClassVar[frozenset[str]] = frozenset()
    #: Request headers that carry the same.
    masked_headers: ClassVar[frozenset[str]] = frozenset()
    #: The variable the core sets to a fresh token when each request names its
    #: own upstream; ``None`` when the tool cannot.
    upstream_per_request: ClassVar[str | None] = None

    def expand(self, target: str, args: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        """A target syntax of the type's own, such as ``ollama:<model>``, as a profile and args."""
        return None

    def endpoint(self, launch: LaunchContext) -> Endpoint | None:
        """Where the tool would send requests on its own; the core decides whether to follow it.

        Reads only the tool's configuration: a launch is planned before any
        resource is opened.
        """
        return None

    def argv(self, base_url: str, argv: list[str], endpoint: Endpoint | None) -> list[str]:
        """``argv`` with what points the tool at ``base_url``: its config may outrank a variable."""
        return argv

    def env(self, launch: LaunchContext) -> dict[str, str]:
        """Variables the tool needs beside the proxy URL; ``env_vars`` and the profile win."""
        return {}

    def check(self, upstream: str) -> None:
        """Fail before launch when the tool cannot work with ``upstream``.

        Raises:
            PrivyxError: Saying what to do instead.
        """
        return None
