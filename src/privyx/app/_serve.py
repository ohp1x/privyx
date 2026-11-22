"""Serving an app: its ASGI stack, what runs while it serves, and the server around it.

:func:`compose` builds the stack :meth:`Privyx.asgi <privyx.app.Privyx.asgi>`
returns, the edge middleware around the router; :func:`running` opens the
features, the components, the provider, and the pipeline for it, and closes
them again.  An outside server runs :func:`running` as the app's lifespan;
:func:`serve`, behind ``privyx proxy`` and :meth:`Privyx.serve`, runs it before
uvicorn starts, so a config that cannot be built fails as an error, not as a
server that never comes up.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.events import AppStarting, AppStopping

if TYPE_CHECKING:
    from fastapi import FastAPI

    from privyx.app._privyx import Privyx
    from privyx.config.schema import Settings
    from privyx.core import EventBus
    from privyx.dialects import Dialect
    from privyx.engine import PrivacyEngine
    from privyx.observability.audit import AuditLogger
    from privyx.providers import Provider
    from privyx.proxy import Pipeline

#: Seconds the requests still in flight get to finish on SIGTERM or a --reload
#: restart; then they are cancelled, which still deletes their ephemeral
#: sessions.  Within the 10 s `docker stop` waits before it sends SIGKILL.
SHUTDOWN_GRACE_SECONDS = 5


def compose(
    app: Privyx, settings: Settings, *, lifespan: bool = True, **running_options: Any
) -> tuple[Any, FastAPI]:
    """The app's ASGI stack, and the router at its center.

    The plugins the config names are set up first, so their routes and
    middleware are in it.  With ``lifespan``, the router opens what it serves
    with as it starts (:func:`running`, given ``running_options``); without,
    the caller runs :func:`running` around the server.

    Raises:
        ConfigError: If a plugin cannot be set up, or one of its routes
            overlaps a masking route.
    """
    from privyx.app._plugins import load_plugins
    from privyx.proxy._asgi import create_app

    load_plugins(settings, app)
    routes = masked_routes(app, settings)
    kinds = dialect_types(app, settings.proxy.routes)

    @asynccontextmanager
    async def opened(router: FastAPI) -> AsyncIterator[None]:
        # Yields no lifespan state: what it opens is in the router's own state.
        async with running(app, settings, router, **running_options):
            yield

    router = create_app(
        settings=settings,
        router=app._router,
        served=routes if settings.proxy.mode == "gateway" else None,
        versionless=frozenset(name for name, kind in kinds.items() if kind.versionless),
        lifespan=opened if lifespan else None,
    )
    stack: Any = router
    # Lowest priority innermost; of equal ones, the one added first is outer.
    for _, _, factory, options in sorted(app._middleware, key=lambda m: (m[0], -m[1])):
        stack = factory(stack, **options)
    return stack, router


@asynccontextmanager
async def running(
    app: Privyx,
    settings: Settings,
    router: FastAPI,
    *,
    features: Mapping[str, Mapping[str, Any]] | None = None,
    run_token: str | None = None,
) -> AsyncIterator[Pipeline]:
    """Open what ``router`` serves with, and close it on exit, in reverse order.

    The features the settings switch on, and ``features``, the engine's
    components, the provider, and the pipeline are opened, in
    ``router.state`` for its routes, and then
    :class:`~privyx.events.AppStarting` is emitted; on exit
    :class:`~privyx.events.AppStopping`, then the rest is closed.

    Raises:
        ConfigError: If a component or a feature cannot be built or opened, or
            a listener of :class:`~privyx.events.AppStarting` fails.
    """
    from privyx.agents import Agent, client_metadata
    from privyx.app._build import build_engine, open_provider

    async with AsyncExitStack() as stack:
        audit = await stack.enter_async_context(app.features.running(settings, extra=features))
        engine, close_engine = await build_engine(settings, app, audit=audit)
        stack.push_async_callback(close_engine)
        provider = await open_provider(settings, app)
        stack.push_async_callback(provider.close)
        pipeline = build_pipeline(
            settings,
            engine,
            provider,
            audit,
            events=app._events,
            run_token=run_token,
            agents=client_metadata(app.registry(Agent)),
            dialects={
                name: kind() for name, kind in dialect_types(app, settings.proxy.routes).items()
            },
        )
        stack.push_async_callback(pipeline.close)
        router.state.pipeline = pipeline
        router.state.engine = engine
        router.state.metrics = app.features.get("metrics")
        await app._events.emit(AppStarting())
        stack.push_async_callback(app._events.emit, AppStopping())
        yield pipeline


def build_pipeline(
    settings: Settings,
    engine: PrivacyEngine,
    provider: Provider,
    audit: AuditLogger | None = None,
    *,
    events: EventBus | None = None,
    run_token: str | None = None,
    agents: tuple[frozenset[str], frozenset[str]] | None = None,
    dialects: Mapping[str, Dialect] | None = None,
) -> Pipeline:
    """The pipeline ``settings`` describe, for both proxy modes.

    ``run_token`` is ``privyx run pi``'s: each request names its own upstream.
    ``events`` is the bus the request events go to, the app's.  ``agents``
    holds the request keys and headers clients describe their machine in
    (:func:`~privyx.agents.client_metadata`), and ``dialects`` the dialect of each route
    (:func:`dialect_types`); by default, the built-ins.

    Raises:
        ConfigError: If ``provider.path_mapping`` cannot join a path to the upstream.
    """
    from privyx.providers import resolve_transparent_upstream
    from privyx.proxy import Pipeline
    from privyx.proxy._tap import Tap

    proxy, upstream = settings.proxy, settings.provider
    gateway = proxy.mode == "gateway"
    mapping = upstream.path_mapping
    metadata_keys, masked_headers = agents or (None, None)
    return Pipeline(
        engine,
        provider,
        gateway=gateway,
        # Each request names its own upstream with a run token: none to resolve.
        origin="" if gateway or run_token else resolve_transparent_upstream(settings),
        strip_prefix=mapping.strip_prefix if mapping.mode == "prefix" else None,
        routes=dict(proxy.routes),
        passthrough_unknown=proxy.passthrough_unknown,
        forward_client_auth=upstream.forward_client_auth,
        timeout=upstream.timeout,
        connect_timeout=upstream.connect_timeout,
        max_connections=upstream.max_connections,
        audit=audit,
        session_strategy=settings.session.strategy,
        tap=Tap(settings.proxy.tap_dir) if settings.proxy.tap else None,
        run_token=run_token,
        metadata_keys=metadata_keys,
        masked_headers=masked_headers,
        dialects=dialects,
        events=events,
    )


def add_default_routes(app: Privyx, settings: Settings) -> None:
    """Add the routes each plugin's dialect brings to the default route map, in place.

    A route map written in the config replaces the default one, and is taken
    as written.

    Raises:
        ConfigError: If a dialect's route is a path the map has already.
    """
    from privyx.config.schema import ProxyConfig
    from privyx.core import ConfigError
    from privyx.dialects import Dialect

    routes = settings.proxy.routes
    if routes != ProxyConfig().routes:
        return
    registry = app.registry(Dialect)
    for name in registry.names():
        for path in [] if registry.is_builtin(name) else registry.get(name).default_routes:
            if path in routes:
                raise ConfigError(
                    f"dialect {name!r} brings route {path}, which is {routes[path]!r}'s"
                )
            routes[path] = name


def masked_routes(app: Privyx, settings: Settings) -> dict[str, str]:
    """The paths the proxy masks as ``settings`` have it, each with its dialect's name.

    The default map with the routes plugin dialects bring; for the gateway,
    only the ones it serves with the provider selected.

    Raises:
        ConfigError: If a dialect's route is a path the map has already, a
            route names no dialect, or the gateway's provider type is unknown.
    """
    from privyx.providers import GoogleProvider, Provider
    from privyx.proxy import served_routes

    add_default_routes(app, settings)
    routes = dict(settings.proxy.routes)
    if settings.proxy.mode != "gateway":
        return routes
    provider = app.registry(Provider).get(settings.provider.type)
    return served_routes(
        routes, dialect_types(app, routes), google=issubclass(provider, GoogleProvider)
    )


def dialect_types(app: Privyx, routes: Mapping[str, str]) -> dict[str, type[Dialect]]:
    """The dialect type each route of ``routes`` names, by name.

    Raises:
        ConfigError: If a route names no dialect ``app`` has.
    """
    from privyx.dialects import Dialect

    registry = app.registry(Dialect)
    return {name: registry.get(name) for name in dict.fromkeys(routes.values())}


def _tls(settings: Settings) -> dict[str, Any]:
    """uvicorn's TLS settings: none unless ``server.tls`` names a certificate."""
    tls = settings.server.tls
    return {
        "ssl_certfile": tls.certfile or None,
        "ssl_keyfile": tls.keyfile or None,
        "ssl_keyfile_password": tls.keyfile_password or None,
        "ssl_ca_certs": tls.ca_certs or None,
    }


#: The variable that tells a server process what to serve: the parent's
#: config file, overrides, and presets.
_WORKER = "PRIVYX_WORKER"


def serve_workers(app: Privyx) -> None:
    """Serve ``app`` from ``server.workers`` processes, each with the app built again.

    A process builds the app from the config, the overrides, and the
    presets, so what code added to ``app`` is in none of them: such an app
    serves with one process, or with ``uvicorn --factory --workers N`` and
    a factory of its own.  The vault must be one they all share.

    Raises:
        ConfigError: If the vault keeps its sessions in one process (``memory``,
            or a sqlite ``:memory:``), a feature keeps state in its instance,
            code added to ``app``, or the app cannot be built.
    """
    import uvicorn

    from privyx.config import CHOSEN_PRESETS
    from privyx.core import ConfigError
    from privyx.features import Feature
    from privyx.observability.logging import configure_logging

    settings = app.settings
    workers = settings.server.workers
    if not app.sessions.persistent:
        raise ConfigError(
            f"server.workers {workers} needs a vault the processes share; "
            "set vault.type to redis, or to sqlite with a file in vault.dsn"
        )
    registry = app.registry(Feature)
    for name, options in app.features.configured(settings).items():
        if registry.get(options.get("type", name)).single_process:
            raise ConfigError(
                f"feature {name!r} keeps its state in one process; "
                "turn it off or serve with server.workers 1"
            )
    if app._in_code:
        raise ConfigError(
            "server.workers builds each process from the config, without what code "
            "added to the app; serve it with one, or with uvicorn --factory --workers"
        )
    configure_logging(settings)
    asyncio.run(_trial(app, settings))  # a config that cannot be built fails here, once
    config = str(Path(app._config).absolute()) if app._config is not None else None
    os.environ[_WORKER] = json.dumps(
        {"config": config, "overrides": app._overrides, "presets": list(CHOSEN_PRESETS.get())}
    )
    scheme = "https" if settings.server.tls.enabled else "http"
    print(
        f"Privyx proxy listening on {scheme}://{settings.server.host}:{settings.server.port} "
        f"with {workers} workers",
        flush=True,
    )
    uvicorn.run(
        "privyx.app._serve:worker",
        factory=True,
        host=settings.server.host,
        port=settings.server.port,
        workers=workers,
        log_level=settings.log.level,
        ws="none",
        **_tls(settings),
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )


async def _trial(app: Privyx, settings: Settings) -> None:
    """Open the components, the features, and the provider ``settings`` describe, and close them."""
    from privyx.app._build import build_engine, open_provider
    from privyx.app._plugins import load_plugins

    load_plugins(settings, app)
    async with app.features.running(settings):
        engine, close = await build_engine(settings, app)
        await close()
        provider = await open_provider(settings, app)
        await provider.close()


def worker() -> Any:
    """The app one process of :func:`serve_workers` serves: ``uvicorn --factory``."""
    from privyx.app._privyx import Privyx
    from privyx.config import CHOSEN_PRESETS
    from privyx.observability.logging import configure_logging

    spec = json.loads(os.environ[_WORKER])
    CHOSEN_PRESETS.set(tuple(spec["presets"]))
    app = Privyx(spec["config"], **spec["overrides"])
    configure_logging(app.settings)
    return app.asgi()


async def serve(app: Privyx, watch: Sequence[Path] = ()) -> bool:
    """Serve ``app`` as ``privyx proxy`` does, until stopped or one of ``watch`` changes.

    Returns:
        Whether a file of ``watch`` changed.

    Raises:
        ConfigError: If the app cannot be built.
    """
    import uvicorn

    from privyx.observability.logging import configure_logging

    settings = app.settings
    configure_logging(settings)
    asgi, router = compose(app, settings, lifespan=False)
    changed = asyncio.Event() if watch else None
    async with running(app, settings, router) as pipeline:
        for line in _banner(settings, pipeline):
            print(line, flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                asgi,
                host=settings.server.host,
                port=settings.server.port,
                log_level=settings.log.level,
                ws="none",  # a WebSocket upgrade arrives as a plain HTTP request
                **_tls(settings),
                timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
            )
        )
        watcher = asyncio.create_task(_watch(watch, changed)) if changed is not None else None
        try:
            await serve_until(server, changed)
        finally:
            if watcher is not None:
                watcher.cancel()
    return changed is not None and changed.is_set()


def _banner(settings: Settings, pipeline: Pipeline) -> list[str]:
    """What ``privyx proxy`` prints as it starts: where it listens and where requests go."""
    from privyx.providers import resolve_base_url, resolve_transparent_upstream
    from privyx.security import redact

    scheme = "https" if settings.server.tls.enabled else "http"
    where = f"{scheme}://{settings.server.host}:{settings.server.port}"
    if pipeline.gateway:
        routes = pipeline.routes
        lines = [
            f"Privyx proxy listening on {where}",
            f"Upstream: {redact(resolve_base_url(settings), 'url')}",
        ]
    else:
        routes = dict(settings.proxy.routes)
        origin = redact(resolve_transparent_upstream(settings), "url")
        mapping = settings.provider.path_mapping
        lines = [
            f"Privyx transparent proxy listening on {where}",
            f"Upstream: {origin} (path mapping: prefix, strip {mapping.strip_prefix or 'nothing'})"
            if mapping.mode == "prefix"
            else f"Upstream origin: {origin}",
        ]
    return [
        *lines,
        f"Routes: {', '.join(f'{path}→{schema}' for path, schema in routes.items())}",
        f"Engine: detector={settings.detector_type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type} "
        f"session={settings.session.strategy}",
    ]


async def serve_until(server: Any, event: asyncio.Event | None) -> None:
    """Serve until the server stops on its own, or until ``event`` fires."""
    # uvicorn stops on SIGINT or SIGTERM, then raises it again for the handler
    # it found, which kills the process or cancels this task before the
    # requests it cut off have cleaned up and the vault and the audit log have
    # closed.  Its own handler there only asks it to stop, again.
    stops = (signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.signal(sig, server.handle_exit) for sig in stops}
    try:
        if event is None:
            await server.serve()
            return
        serving = asyncio.create_task(server.serve())
        waiting = asyncio.create_task(event.wait())
        await asyncio.wait({serving, waiting}, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True  # graceful uvicorn shutdown
        waiting.cancel()
        await serving
    finally:
        # uvicorn cancels the requests still running after the grace period but
        # does not wait for them, and their cleanup (the ephemeral session's
        # deletion, the last audit events) must end before the vault and the
        # audit log close.
        cancelled = set(server.server_state.tasks)
        if cancelled:
            await asyncio.wait(cancelled, timeout=2)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def mtimes(paths: Sequence[Path]) -> tuple[int, ...] | None:
    """Modification times, or ``None`` while a file is momentarily missing."""
    try:
        return tuple(path.stat().st_mtime_ns for path in paths)
    except OSError:
        return None


async def wait_for_change(paths: Sequence[Path]) -> None:
    """Return once a file of ``paths`` changes on disk.

    An mtime poll each second; watchfiles if that ever matters.
    """
    last = mtimes(paths)
    while True:
        await asyncio.sleep(1.0)
        now = mtimes(paths)
        if now is not None and now != last:
            return


async def _watch(paths: Sequence[Path], event: asyncio.Event) -> None:
    """Set ``event`` once a file of ``paths`` changes on disk."""
    await wait_for_change(paths)
    event.set()
