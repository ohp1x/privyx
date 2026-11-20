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

A package does the same in a ``setup(px)`` function that ``plugins.modules``
names.  The interface may change in a 0.x release.

Plugins are registered in one process-wide registry, so a process holds one
``Privyx``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core import Event, EventBus

if TYPE_CHECKING:
    from fastapi import APIRouter

    from privyx.config.schema import Settings
    from privyx.engine import PrivacyEngine


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
        self._components: list[type] = []
        self._events = EventBus()
        self._router: APIRouter | None = None
        self._modules: set[str] = set()

    def add[T: type](self, cls: T) -> T:
        """Add component ``cls``; usable as a class decorator.

        Its family follows from its base class and its type from its ``name``,
        as for a plugin in ``plugins.paths``.

        Raises:
            ConfigError: If ``cls`` is not a component class.
        """
        from privyx.app._plugins import family

        family(cls)
        self._components.append(cls)
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
        from privyx.config.loader import load_config

        return load_config(self._config, extra=self._overrides or None)

    def run(self) -> None:
        """Serve the proxy, as ``privyx proxy`` does, until stopped."""
        from privyx.cli.commands.proxy import _run_server

        asyncio.run(_run_server(self._settings(), app=self))

    @asynccontextmanager
    async def engine(self) -> AsyncIterator[PrivacyEngine]:
        """The privacy engine, with this ``Privyx``'s components; the vault closes on exit."""
        from privyx.app._plugins import load_plugins
        from privyx.core.builder import build_engine

        settings = self._settings()
        load_plugins(settings, app=self)
        engine, close = await build_engine(settings)
        try:
            yield engine
        finally:
            await close()
