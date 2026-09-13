"""Plugin registry — maps component names to factories.

A single generic registry backs every pluggable component (detectors,
operators, policies, vaults, providers, stream adapters).  Resolving names
through a registry is what keeps ``if provider == "openai"`` branches out of
the rest of the codebase: to add a backend you register a factory, you do not
edit a dispatch table.

Example::

    registry: Registry[Operator] = Registry("operator")
    registry.register("redact", lambda cfg: RedactOperator(cfg.get("token", "[REDACTED]")))
    operator = registry.build({"type": "redact"})
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from privyx.core.errors import ConfigError

#: A factory takes the component's config dict and returns an instance.
Factory = Callable[[dict[str, Any]], Any]


class Registry[T]:
    """Name → factory map for one kind of pluggable component.

    Args:
        kind: Human-readable component name, used in error messages.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: dict[str, Callable[[dict[str, Any]], T]] = {}

    def register(self, name: str, factory: Callable[[dict[str, Any]], T]) -> None:
        """Register ``factory`` under ``name``, replacing any existing entry."""
        self._factories[name] = factory

    def build(self, config: dict[str, Any] | None = None, *, default: str = "") -> T:
        """Instantiate the component named by ``config["type"]``.

        Args:
            config: Component config; ``type`` selects the factory and the rest
                is passed through to it.
            default: Type to use when ``config`` omits one.

        Raises:
            ConfigError: If the config is not a mapping or names an unknown type.
        """
        config = config or {}
        if not isinstance(config, dict):
            raise ConfigError(f"invalid {self._kind} config: {config!r}")
        name = config.get("type", default)
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(
                f"unknown {self._kind} type: {name!r} (available: {', '.join(self.names())})"
            )
        return factory(config)

    def names(self) -> list[str]:
        """Return the registered type names, sorted."""
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories
