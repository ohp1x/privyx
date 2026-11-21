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

    px.serve()

:meth:`Privyx.asgi` gives the same app to another ASGI server, or to a parent
app that mounts it: ``uvicorn --factory acme.app:make_app``, where
``make_app`` returns ``Privyx(...).asgi()``.

A package does the same in a ``setup(app)`` function that ``plugins.modules``
names, and a file of ``plugins.paths`` in its own.  The interface may change
in a 0.x release.

Each ``Privyx`` has registries of its own: two in one process do not see each
other's components.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from privyx.core import Component, ConfigError, Event, EventBus, Registry

if TYPE_CHECKING:
    from fastapi import APIRouter

    from privyx.app._services._audit import Audit
    from privyx.app._services._features import Features
    from privyx.app._services._masking import Masking
    from privyx.app._services._sessions import Sessions
    from privyx.app._services._workspace import Workspace
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
    "privyx.secrets",
    "privyx.agents",
    "privyx.dialects",
    "privyx.features",
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
        #: Edge middleware: (priority, order added, factory, options).
        self._middleware: list[tuple[int, int, Callable[..., Any], dict[str, Any]]] = []
        self._loaded: Settings | None = None
        self._engine: PrivacyEngine | None = None
        # The services, each made when first used.
        self._masking: Masking | None = None
        self._workspace: Workspace | None = None
        self._sessions: Sessions | None = None
        self._audit_service: Audit | None = None
        self._features: Features | None = None
        self._modules: set[str] = set()
        self._files: set[Path] = set()
        #: What ``defaults()`` added, by section and name, and who added each.
        self._defaults: dict[str, dict[str, Any]] = {}
        self._defaults_from: dict[tuple[str, str], str] = {}
        #: The plugin being set up, named in errors.
        self._setting_up = "the app"
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
        is logged.  Both are emitted as the app is served (:meth:`serve`,
        :meth:`asgi`, ``privyx proxy``, ``privyx run``), not by :meth:`engine`.
        """

        def add(listener: F) -> F:
            self._events.add(event, listener, priority=priority)
            return listener

        return add

    def defaults(
        self,
        *,
        agents: Mapping[str, Mapping[str, Any]] | None = None,
        presets: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Add agent profiles and presets, as if written just above Privyx's own defaults.

        Every config file still overrides them, field by field.  Call it in
        ``setup(app)``, or before the settings are read.

        Raises:
            ConfigError: If a name is a built-in profile's, or another plugin
                added it already.
        """
        from privyx.config.defaults import DEFAULT_AGENTS

        for section, entries in (("agents", agents), ("presets", presets)):
            for name, value in (entries or {}).items():
                if section == "agents" and name in DEFAULT_AGENTS:
                    raise ConfigError(
                        f"agent profile {name!r} is built in; {self._setting_up} "
                        "must give its own a name of its own"
                    )
                other = self._defaults_from.get((section, name))
                if other is not None:
                    raise ConfigError(
                        f"two plugins add {section}.{name}: {other} and {self._setting_up}"
                    )
                self._defaults_from[(section, name)] = self._setting_up
                self._defaults.setdefault(section, {})[name] = dict(value)

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

    def add_middleware(
        self, factory: Callable[..., Any], *, priority: int = 0, **options: Any
    ) -> None:
        """Serve every request through ASGI middleware ``factory``, before the routes.

        ``factory(app, **options)`` wraps the app inside it, as Starlette's
        ``CORSMiddleware`` does: it sees every route, ``/health`` and plugin
        routes too, before a body is read.  The highest ``priority`` is
        outermost; of equal ones, the one added first.
        """
        self._middleware.append((priority, len(self._middleware), factory, options))

    @property
    def settings(self) -> Settings:
        """The settings: the config files, the environment, then the overrides; read once."""
        if self._loaded is None:
            self._loaded = self._load(self._config, extra=self._overrides or None)
        return self._loaded

    def _load(
        self, path: str | Path | None = None, *, secrets: bool = True, **options: Any
    ) -> Settings:
        """:func:`privyx.config.load_config` with this app's types and plugins.

        The plugins the config names are set up first, then the defaults they
        add are merged below the config files, then the secret references
        are resolved, with the plugins' resolvers too; with ``secrets=False``
        they are left as written.
        """
        from privyx.config import load_config

        return load_config(
            path,
            types=self._registries,
            setup=self._set_up,
            secrets=self._resolve if secrets else None,
            **options,
        )

    def _set_up(self, merged: dict[str, Any]) -> dict[str, Any]:
        """Set up the plugins a merged config names; the defaults they added."""
        from pydantic import ValidationError

        from privyx.app._plugins import set_up_plugins
        from privyx.config.schema import PluginsConfig

        try:
            plugins = PluginsConfig.model_validate(merged.get("plugins") or {})
        except ValidationError:
            return {}  # reported with the rest of the settings
        set_up_plugins(plugins, self)
        return self._defaults

    def _resolve(self, merged: dict[str, Any]) -> dict[str, Any]:
        """``merged`` with each secret reference replaced by the secret it names.

        Raises:
            ConfigError: If a ``secrets`` entry names no resolver or has an
                invalid option, or a reference cannot be resolved.
        """
        from privyx.secrets import SecretResolver, resolve_references

        registry = self.registry(SecretResolver)
        options = merged.get("secrets")
        configured = options if isinstance(options, dict) else {}
        built = {
            scheme: registry.build(scheme, value or {}, setting=f"secrets.{scheme}")
            for scheme, value in configured.items()
        }

        def resolver(scheme: str) -> SecretResolver | None:
            if scheme not in built and scheme in registry:
                built[scheme] = registry.build(scheme, {}, setting=f"secrets.{scheme}")
            return built.get(scheme)

        resolved: dict[str, Any] = resolve_references(merged, resolver)
        return resolved

    def asgi(self) -> Any:
        """The app as ASGI: its middleware around its routes and the proxy's.

        Its lifespan opens the components as the server starts and closes them
        as it stops; a parent app that mounts it must run that lifespan too
        (a Starlette ``Mount`` does not).

        Raises:
            ConfigError: If the settings cannot be read, a plugin cannot be set
                up, or a plugin route overlaps a masking route.
        """
        from privyx.app._serve import compose

        return compose(self, self.settings)[0]

    def serve(self, *, watch: Sequence[Path] = ()) -> bool:
        """Serve the proxy, as ``privyx proxy`` does, until stopped.

        With ``watch``, also until one of those files changes on disk, and then
        return ``True`` (``privyx proxy --reload``).

        Raises:
            ConfigError: If the app cannot be built.
        """
        from privyx.app._serve import serve

        return asyncio.run(serve(self, watch))

    @asynccontextmanager
    async def engine(self) -> AsyncIterator[PrivacyEngine]:
        """The privacy engine, with this ``Privyx``'s components; they close on exit.

        Inside another ``engine()`` of this app, the same engine, which the
        outer one closes: a server holds one open for all its calls.
        """
        from privyx.app._build import build_engine
        from privyx.app._plugins import load_plugins

        if self._engine is not None:
            yield self._engine
            return
        settings = self.settings
        load_plugins(settings, self)
        engine, close = await build_engine(settings, self)
        self._engine = engine
        try:
            yield engine
        finally:
            self._engine = None
            await close()

    @property
    def masking(self) -> Masking:
        """Masking and restoring text and files, and detection (experimental)."""
        if self._masking is None:
            from privyx.app._services._masking import Masking

            self._masking = Masking(self)
        return self._masking

    @property
    def workspace(self) -> Workspace:
        """The working directory: its scan, and the checks of its git hooks (experimental)."""
        if self._workspace is None:
            from privyx.app._services._workspace import Workspace

            self._workspace = Workspace(self)
        return self._workspace

    @property
    def sessions(self) -> Sessions:
        """The vault's sessions: list, show, export, prune (experimental)."""
        if self._sessions is None:
            from privyx.app._services._sessions import Sessions

            self._sessions = Sessions(self)
        return self._sessions

    @property
    def features(self) -> Features:
        """The features: which the settings switch on, those running, and their listeners."""
        if self._features is None:
            from privyx.app._services._features import Features

            self._features = Features(self)
        return self._features

    @property
    def audit(self) -> Audit:
        """The audit trail and the tap: totals, recent events, records (experimental)."""
        if self._audit_service is None:
            from privyx.app._services._audit import Audit

            self._audit_service = Audit(self)
        return self._audit_service


def load_config(path: str | Path | None = None, **options: Any) -> Settings:
    """Load the settings, with the options of each built-in component type checked.

    The arguments are :func:`privyx.config.load_config`'s.  A plugin's
    options are checked when its component is built.

    Raises:
        ConfigError: If a file cannot be read, a setting is unknown or
            invalid, or a reference cannot be resolved.
    """
    return Privyx()._load(path, **options)
