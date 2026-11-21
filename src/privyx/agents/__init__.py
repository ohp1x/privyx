"""Agents: the tools ``privyx run`` launches, and how each is pointed at the proxy."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from privyx.agents._base import Agent, Endpoint, LaunchContext
from privyx.core import Registry, imported, lazy_attribute

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.agents._claude import ClaudeAgent
    from privyx.agents._codex import CodexAgent
    from privyx.agents._env import EnvAgent
    from privyx.agents._ollama import OllamaAgent
    from privyx.agents._pi import PiAgent
    from privyx.agents._plan import LaunchPlan
    from privyx.agents._plan import Target as Target
    from privyx.agents._plan import build_launch_plan as build_launch_plan

__all__ = [
    "Agent",
    "builtin_metadata",
    "client_metadata",
    "ClaudeAgent",
    "CodexAgent",
    "Endpoint",
    "EnvAgent",
    "LaunchContext",
    "LaunchPlan",
    "OllamaAgent",
    "PiAgent",
]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "env": "privyx.agents._env:EnvAgent",
    "claude": "privyx.agents._claude:ClaudeAgent",
    "codex": "privyx.agents._codex:CodexAgent",
    "pi": "privyx.agents._pi:PiAgent",
    "ollama": "privyx.agents._ollama:OllamaAgent",
}

#: Outside ``__all__``, the plan's own pieces, for the conformance suite.
_LAZY = {
    **_BUILTINS,
    "plan": "privyx.agents._plan:LaunchPlan",
    "target": "privyx.agents._plan:Target",
    "build": "privyx.agents._plan:build_launch_plan",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.agents import CodexAgent`` imports that module on first use."""
    return lazy_attribute(name, _LAZY, __name__)


def client_metadata(registry: Registry[Agent]) -> tuple[frozenset[str], frozenset[str]]:
    """The request keys and headers the clients of ``registry``'s types describe their machine in.

    The proxy cannot tell which tool sent a request, so it masks them all.
    """
    return _union([registry.get(name) for name in registry.names()])


@cache
def builtin_metadata() -> tuple[frozenset[str], frozenset[str]]:
    """:func:`client_metadata` of the built-in types: a proxy built without an app masks those."""
    return _union([imported(path) for path in _BUILTINS.values()])


def _union(types: list[type[Agent]]) -> tuple[frozenset[str], frozenset[str]]:
    keys = frozenset[str]().union(*(kind.metadata_keys for kind in types))
    return keys, frozenset[str]().union(*(kind.masked_headers for kind in types))


def setup(app: Privyx) -> None:
    """Register the built-in agent types with ``app``."""
    app.registry(Agent).builtins(_BUILTINS)
