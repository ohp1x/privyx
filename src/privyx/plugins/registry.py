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
from typing import TYPE_CHECKING, Any

from privyx.core.errors import ConfigError

if TYPE_CHECKING:
    from privyx.privacy.anchor.base import Anchor
    from privyx.privacy.detector.base import Detector
    from privyx.privacy.operator.base import Operator
    from privyx.privacy.policy.base import Policy
    from privyx.providers.base import Provider
    from privyx.vault.base import Vault

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

    def clear(self) -> None:
        """Forget every registration."""
        self._factories.clear()

    def __contains__(self, name: object) -> bool:
        return name in self._factories


class PluginRegistry:
    """One :class:`Registry` per pluggable component family.

    Populated by :func:`privyx.plugins.loader.load_plugins` and consulted as a
    *fallback* by the ``build_*`` functions when a config ``type`` is not one of
    the built-ins.  Keeping plugin factories in a separate registry (rather than
    merging them into the built-in dispatch) means a broken or missing plugin
    can never shadow a built-in component.
    """

    def __init__(self) -> None:
        self.detectors: Registry[Detector] = Registry("detector")
        self.operators: Registry[Operator] = Registry("operator")
        self.policies: Registry[Policy] = Registry("policy")
        self.providers: Registry[Provider] = Registry("provider")
        self.anchors: Registry[Anchor] = Registry("anchor")
        self.vaults: Registry[Vault] = Registry("vault")

    def clear(self) -> None:
        """Reset every family to empty (called at the start of each load)."""
        for registry in (
            self.detectors,
            self.operators,
            self.policies,
            self.providers,
            self.anchors,
            self.vaults,
        ):
            registry.clear()


#: Process-wide plugin registry.  ``load_plugins`` clears and repopulates it;
#: the ``build_*`` functions read it.  A module-level singleton is what lets a
#: plugin registered at startup be seen by every later build call.
PLUGINS = PluginRegistry()
