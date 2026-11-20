"""``Privyx``: assemble and extend Privyx from Python (experimental).

A program builds one :class:`Privyx`, adds its own components, routes, and
listeners to it, and then serves it as ``privyx proxy`` would, or takes the
engine alone::

    from privyx import Privyx
    from privyx.events import AppStarting

    px = Privyx("privyx.yaml", port=8080)
    px.add(AcmeIdDetector)

    @px.router.get("/acme/status")
    async def status() -> dict[str, str]:
        return {"status": "ok"}

    @px.on(AppStarting)
    async def warm(event: AppStarting) -> None:
        ...

    px.run()

A package does the same in a ``setup(app)`` function that ``plugins.modules``
names, and a file of ``plugins.paths`` in its own.  The interface may change
in a 0.x release.

Each ``Privyx`` has registries of its own: two in one process do not see each
other's components.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from privyx.core import Component, ConfigError, Event, EventBus, Registry

if TYPE_CHECKING:
    from fastapi import APIRouter

    from privyx.config.schema import Settings
    from privyx.engine import PrivacyEngine

#: The packages of the kinds Privyx builds, each with a ``setup(app)`` that
#: registers its built-ins.
_KINDS = (
    "privyx.vaults",
    "privyx.anchors",
    "privyx.policies",
    "privyx.providers",
    "privyx.detectors",
    "privyx.operators",
)


class Privyx:
    """Privyx assembled in code: config, components, routes, and listeners.

    Args:
        config: A config file read alone, as with ``--config``; ``None`` reads
            the usual files.
        **overrides: Settings that win over the files and the environment, as
            command-line flags do: ``port=8080``, ``detector={"terms": ...}``.
    """

    def __init__(self, config: str | Path | None = None, **overrides: Any) -> None:
        self._config = config
        self._overrides = overrides
        self._registries: dict[str, Registry[Any]] = {}
        self._events = EventBus()
        self._router: APIRouter | None = None
        self._modules: set[str] = set()
        self._files: set[Path] = set()
        for package in _KINDS:
            importlib.import_module(package).setup(self)

    def registry[C: Component](self, kind: type[C]) -> Registry[C]:
        """The registry of ``kind``, a kind's base class such as ``Vault``, or a component of it.

        Raises:
            ConfigError: If ``kind`` belongs to no kind.
        """
        name: str | None = getattr(kind, "kind", None)
        if name is None:
            raise ConfigError(f"{kind.__name__} is not a component of any kind")
        registry = self._registries.get(name)
        if registry is None:
            base = next(c for c in kind.__mro__ if "kind" in c.__dict__)
            registry = self._registries[name] = Registry(cast("type[C]", base))
        return registry

    def add[T: type[Component]](self, cls: T) -> T:
        """Add component ``cls`` under its ``name``; usable as a class decorator.

        Its kind follows from its base class.

        Raises:
            ConfigError: If ``cls`` is not a component, is abstract, has no
                name of its own, or another component of its kind has its name.
        """
        if not (isinstance(cls, type) and issubclass(cls, Component)):
            raise ConfigError(f"{cls!r} is not a Privyx component")
        self.registry(cls).add(cls)
        return cls

    def use(self, module: str) -> None:
        """Import plugin ``module`` and call its ``setup`` with this ``Privyx``, once.

        Raises:
            ConfigError: If it cannot be imported, has no ``setup``, or its
                ``setup`` fails.
        """
        from privyx.app._plugins import set_up_module

        if module not in self._modules:
            set_up_module(module, self)
            self._modules.add(module)

    def on[F: Callable[..., Any]](
        self, event: type[Event], *, priority: int = 0
    ) -> Callable[[F], F]:
        """Call the decorated function, sync or async, on each ``event``.

        The highest ``priority`` runs first, then the order added.  A listener
        of :class:`~privyx.events.AppStarting` that fails stops the server
        from starting; one of :class:`~privyx.events.AppStopping` that fails
        is logged.  Both are emitted by :meth:`run`, ``privyx proxy``, and
        ``privyx run``, not by :meth:`engine`.
        """

        def add(listener: F) -> F:
            self._events.add(event, listener, priority=priority)
            return listener

        return add

    @property
    def router(self) -> APIRouter:
        """Routes served beside the proxy's, by ``privyx proxy`` and ``privyx run``.

        A handler finds the engine at ``request.app.state.engine`` and the
        settings at ``request.app.state.settings``.  A route that would take a
        path of ``proxy.routes`` stops the server from starting.
        """
        if self._router is None:
            from fastapi import APIRouter

            self._router = APIRouter()
        return self._router

    def _settings(self) -> Settings:
        """Load the settings: the config files, the environment, then the overrides."""
        return self._load(self._config, extra=self._overrides or None)

    def _load(self, path: str | Path | None = None, **options: Any) -> Settings:
        """:func:`privyx.config.loader.load_config` with this app's types checked."""
        from privyx.config.loader import load_config

        return load_config(path, types=self._registries, **options)

    def run(self) -> None:
        """Serve the proxy, as ``privyx proxy`` does, until stopped."""
        from privyx.cli.commands.proxy import _run_server

        asyncio.run(_run_server(self._settings(), app=self))

    @asynccontextmanager
    async def engine(self) -> AsyncIterator[PrivacyEngine]:
        """The privacy engine, with this ``Privyx``'s components; they close on exit."""
        from privyx.app._build import build_engine
        from privyx.app._plugins import load_plugins

        settings = self._settings()
        load_plugins(settings, self)
        engine, close = await build_engine(settings, self)
        try:
            yield engine
        finally:
            await close()


def load_config(path: str | Path | None = None, **options: Any) -> Settings:
    """Load the settings, with the options of each built-in component type checked.

    The arguments are :func:`privyx.config.loader.load_config`'s.  A plugin's
    options are checked when its component is built.

    Raises:
        ConfigError: If a file cannot be read, a setting is unknown or
            invalid, or a reference cannot be resolved.
    """
    return Privyx()._load(path, **options)
