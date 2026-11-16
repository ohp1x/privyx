"""``Privyx``: assemble and extend Privyx from Python (experimental).

A program builds one :class:`Privyx`, adds its own components, routes, and
hooks to it, and then serves it as ``privyx proxy`` would, or takes the engine
alone::

    from privyx import Privyx

    px = Privyx("privyx.yaml", port=8080)
    px.add(AcmeIdDetector)

    @px.router.get("/acme/status")
    async def status() -> dict[str, str]:
        return {"status": "ok"}

    px.run()

A package does the same in a ``setup(px)`` function that ``plugins.modules``
names.  The interface may change in a 0.x release.

Plugins are registered in one process-wide registry, so a process holds one
``Privyx``.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core.errors import ConfigError
from privyx.plugins.hooks import Hook, HookManager

if TYPE_CHECKING:
    from fastapi import APIRouter

    from privyx.config.schema import Settings
    from privyx.core.engine import PrivacyEngine


class Privyx:
    """Privyx assembled in code: config, components, routes, and hooks.

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
        self._hooks = HookManager()
        self._router: APIRouter | None = None
        self._modules: set[str] = set()

    def add[T: type](self, cls: T) -> T:
        """Add component ``cls``; usable as a class decorator.

        Its family follows from its base class and its type from its ``name``,
        as for a plugin in ``plugins.paths``.

        Raises:
            ConfigError: If ``cls`` is not a component class.
        """
        from privyx.plugins.loader import family

        family(cls)
        self._components.append(cls)
        return cls

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

    def on_startup(self, hook: Hook) -> Hook:
        """Run ``hook`` (sync or async) once a served engine is built; a decorator.

        Hooks run in :meth:`run`, ``privyx proxy``, and ``privyx run``, not in
        :meth:`engine`.
        """
        self._hooks.add_startup(_name(hook), hook)
        return hook

    def on_shutdown(self, hook: Hook) -> Hook:
        """Run ``hook`` (sync or async) when serving stops, best-effort; a decorator."""
        self._hooks.add_shutdown(_name(hook), hook)
        return hook

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
        from privyx.core.builder import build_engine
        from privyx.plugins.loader import load_plugins

        settings = self._settings()
        load_plugins(settings, app=self)
        engine, close = await build_engine(settings)
        try:
            yield engine
        finally:
            await close()

    def _set_up(self, module: str) -> None:
        """Call ``module``'s ``setup`` with this ``Privyx``, once."""
        from privyx.plugins.loader import set_up_module

        if module not in self._modules:
            set_up_module(module, self)
            self._modules.add(module)


def include_router(app: Any, router: APIRouter | None, routes: dict[str, str]) -> None:
    """Serve ``router``'s routes in ``app``.

    Called after ``/health`` and ``/metrics`` and before the proxy's own
    routes, so those two stay Privyx's and the rest are matched first.  The
    routes are taken as they are now: one added to ``router`` later is not
    served.

    Raises:
        ConfigError: If a route would take a path of ``routes``, the masking
            routes: requests there would reach the plugin instead of masking.
    """
    if router is None:
        return
    from fastapi import APIRouter
    from starlette.routing import Match

    from privyx.proxy.schemas import detect_schema

    taken = list(router.routes)
    paths = [path for route in taken if isinstance(path := getattr(route, "path", None), str)]
    # Each `*` of a masking route is filled in with `x`, an API version, or a
    # segment of a plugin path: `/v1beta/models/{name}` takes
    # `/v1beta/models/x:generateContent`.
    fillers = {"x", "v1", "v1beta", "v1alpha"}
    for path in paths:
        fillers.update(filter(None, re.split(r"[/:]", _PARAM.sub("", path))))
    examples = [example for masking in routes for example in _filled(masking, fillers)]
    for route in taken:
        # Both ways: each masking path against the route, with the matcher that
        # serves it, and a path the route serves, its parameters filled in,
        # against the masking routes.
        path = getattr(route, "path", None)
        overlaps = any(route.matches(_scope(example))[0] != Match.NONE for example in examples)
        if overlaps or (isinstance(path, str) and detect_schema(_PARAM.sub("x", path), routes)):
            name = repr(path) if isinstance(path, str) else type(route).__name__
            raise ConfigError(f"plugin route {name} overlaps a masking route in proxy.routes")
    app.include_router(APIRouter(routes=taken))


def _scope(path: str) -> dict[str, Any]:
    """The ASGI scope of a ``POST`` to ``path``, as a route's matcher reads it."""
    return {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": []}


def _filled(route: str, fillers: set[str]) -> Iterator[str]:
    """``route`` with each ``*`` replaced by each of ``fillers``."""
    pieces = route.split("*")
    for chosen in itertools.product(sorted(fillers), repeat=len(pieces) - 1):
        yield pieces[0] + "".join(f + piece for f, piece in zip(chosen, pieces[1:], strict=True))


#: A path parameter in a route, ``{name}`` or ``{name:path}``.
_PARAM = re.compile(r"\{[^}]*\}")


def _name(hook: Callable[..., Any]) -> str:
    return str(getattr(hook, "__qualname__", repr(hook)))
