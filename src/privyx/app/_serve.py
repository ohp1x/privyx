"""Serving an app: its ASGI stack, what runs while it serves, and the server around it.

:func:`compose` builds the stack :meth:`Privyx.asgi <privyx.app.Privyx.asgi>`
returns, the edge middleware around the router; :func:`running` opens the
audit trail, the components, the provider, and the pipeline for it, and closes
them again.  An outside server runs :func:`running` as the app's lifespan;
:func:`serve`, behind ``privyx proxy`` and :meth:`Privyx.serve`, runs it before
uvicorn starts, so a config that cannot be built fails as an error, not as a
server that never comes up.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.events import AppStarting, AppStopping

if TYPE_CHECKING:
    from fastapi import FastAPI

    from privyx.app._privyx import Privyx
    from privyx.config.schema import Settings
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
    from privyx.providers import GoogleProvider, Provider
    from privyx.proxy import served_routes
    from privyx.proxy._asgi import create_app

    load_plugins(settings, app)
    served = None
    if settings.proxy.mode == "gateway":
        provider = app.registry(Provider).get(settings.provider.type)
        served = served_routes(
            dict(settings.proxy.routes), google=issubclass(provider, GoogleProvider)
        )

    @asynccontextmanager
    async def opened(router: FastAPI) -> AsyncIterator[None]:
        # Yields no lifespan state: what it opens is in the router's own state.
        async with running(app, settings, router, **running_options):
            yield

    router = create_app(
        settings=settings,
        router=app._router,
        served=served,
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
    audit: AuditLogger | None = None,
    run_token: str | None = None,
) -> AsyncIterator[Pipeline]:
    """Open what ``router`` serves with, and close it on exit, in reverse order.

    The audit trail, the engine's components, the provider, and the pipeline
    are opened, in ``router.state`` for its routes, and then
    :class:`~privyx.events.AppStarting` is emitted; on exit
    :class:`~privyx.events.AppStopping`, then the rest is closed.  An
    ``audit`` logger given is the caller's to close.

    Raises:
        ConfigError: If a component cannot be built or opened, or a listener
            of :class:`~privyx.events.AppStarting` fails.
    """
    from privyx.app._build import build_audit_logger, build_engine, open_provider
    from privyx.observability import tracing

    async with AsyncExitStack() as stack:
        if audit is None:
            audit = build_audit_logger(settings)
            stack.callback(audit.close)
        tracing.configure(settings.tracing.enabled)
        engine, close_engine = await build_engine(settings, app, audit=audit)
        stack.push_async_callback(close_engine)
        provider = await open_provider(settings, app)
        stack.push_async_callback(provider.close)
        pipeline = build_pipeline(settings, engine, provider, audit, run_token=run_token)
        stack.push_async_callback(pipeline.close)
        router.state.pipeline = pipeline
        router.state.engine = engine
        await app._events.emit(AppStarting())
        stack.push_async_callback(app._events.emit, AppStopping())
        yield pipeline


def build_pipeline(
    settings: Settings,
    engine: PrivacyEngine,
    provider: Provider,
    audit: AuditLogger | None = None,
    *,
    run_token: str | None = None,
) -> Pipeline:
    """The pipeline ``settings`` describe, for both proxy modes.

    ``run_token`` is ``privyx run pi``'s: each request names its own upstream.

    Raises:
        ConfigError: If ``proxy.path_mapping`` cannot join a path to the upstream.
    """
    from privyx.providers import resolve_transparent_upstream
    from privyx.proxy import Pipeline
    from privyx.proxy._tap import Tap

    proxy = settings.proxy
    gateway = proxy.mode == "gateway"
    mapping = proxy.path_mapping
    return Pipeline(
        engine,
        provider,
        gateway=gateway,
        # Each request names its own upstream with a run token: none to resolve.
        origin="" if gateway or run_token else resolve_transparent_upstream(settings),
        strip_prefix=mapping.strip_prefix if mapping.mode == "prefix" else None,
        routes=dict(proxy.routes),
        passthrough_unknown=proxy.passthrough_unknown,
        forward_client_auth=proxy.forward_client_auth,
        timeout=proxy.timeout,
        connect_timeout=proxy.connect_timeout,
        max_connections=proxy.max_connections,
        audit=audit,
        session_strategy=settings.session.strategy,
        tap=Tap(settings.audit.tap_dir) if settings.audit.tap else None,
        run_token=run_token,
    )


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
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level,
                ws="none",  # a WebSocket upgrade arrives as a plain HTTP request
                ssl_certfile=settings.tls.certfile or None,
                ssl_keyfile=settings.tls.keyfile or None,
                ssl_keyfile_password=settings.tls.keyfile_password or None,
                ssl_ca_certs=settings.tls.ca_certs or None,
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

    scheme = "https" if settings.is_tls else "http"
    where = f"{scheme}://{settings.host}:{settings.port}"
    if pipeline.gateway:
        routes = pipeline.routes
        lines = [
            f"Privyx proxy listening on {where}",
            f"Upstream: {redact(resolve_base_url(settings), 'url')}",
        ]
    else:
        routes = dict(settings.proxy.routes)
        origin = redact(resolve_transparent_upstream(settings), "url")
        mapping = settings.proxy.path_mapping
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


async def _watch(paths: Sequence[Path], event: asyncio.Event) -> None:
    """Set ``event`` once a file of ``paths`` changes on disk."""
    last = mtimes(paths)
    while True:
        await asyncio.sleep(1.0)
        now = mtimes(paths)
        if now is not None and now != last:
            event.set()
            return
