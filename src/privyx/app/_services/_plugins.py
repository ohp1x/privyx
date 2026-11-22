"""``app.plugins``: the plugins a config loads, what they add, and the ones installed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx


@dataclass(frozen=True)
class Added:
    """A component a plugin added: its kind, its type name, where it is defined."""

    kind: str
    name: str
    source: str
    #: The registered type it extends, if any.
    extends: str | None = None


@dataclass(frozen=True)
class PluginList:
    #: Each plugin the config names: where (``plugins.paths`` or ``.modules``), and which.
    loaded: list[tuple[str, str]]
    components: list[Added]
    #: The ``privyx.plugins`` entry points of the installed packages: name, value.
    installed: list[tuple[str, str]] = field(default_factory=list)


class Plugins:
    def __init__(self, app: Privyx) -> None:
        self._app = app

    def list(self) -> PluginList:
        """The plugins the config names, set up, and the entry points of the installed ones.

        An entry point is read, never loaded.

        Raises:
            ConfigError: If the settings cannot be read, or a plugin cannot be set up.
        """
        from importlib.metadata import entry_points

        from privyx.app._plugins import load_plugins

        settings = self._app.settings
        load_plugins(settings, self._app)
        plugins = settings.plugins
        loaded = (
            [("plugins.paths", p) for p in plugins.paths]
            + [("plugins.modules", m) for m in plugins.modules]
            if plugins.enabled
            else []
        )
        origins = self._app._origins
        components = [
            Added(kind, name, _source(origins.get((kind, name), "")), registry.extends(name))
            for kind, registry in sorted(self._app._registries.items())
            for name in registry.names()
            if not registry.is_builtin(name)
        ]
        installed = sorted((ep.name, ep.value) for ep in entry_points(group="privyx.plugins"))
        return PluginList(loaded, components, installed)


def _source(origin: str) -> str:
    """What added a component: a plugin's file or module, or the app's own code."""
    if origin.startswith("plugin module "):
        return origin.removeprefix("plugin module ").strip("'")
    if origin.startswith("plugin "):
        return origin.removeprefix("plugin ")
    return "code"
