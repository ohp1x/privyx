"""`privyx proxy` command — starts the privacy proxy server."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import click

#: Seconds the requests still in flight get to finish on SIGTERM or a --reload
#: restart; then they are cancelled, which still deletes their ephemeral
#: sessions.  Within the 10 s `docker stop` waits before it sends SIGKILL.
SHUTDOWN_GRACE_SECONDS = 5


@click.command()
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--host", default=None, help="Bind address")
@click.option("--port", default=None, type=int, help="Bind port")
@click.option("--upstream", "-u", default=None, help="Upstream provider URL")
@click.option(
    "--transparent/--gateway",
    "transparent_mode",
    default=None,
    help="Serve a drop-in transparent proxy (all paths) or the chat-only gateway.",
)
@click.option(
    "--reload",
    "reload_on_change",
    is_flag=True,
    default=False,
    help="Restart the server when the config file changes (development).",
)
@click.option("--ssl-certfile", default=None, help="SSL/TLS certificate file path (PEM)")
@click.option("--ssl-keyfile", default=None, help="SSL/TLS private key file path (PEM)")
@click.option("--ssl-keyfile-password", default=None, help="Password for SSL/TLS private key")
@click.option("--ssl-ca-certs", default=None, help="CA certificates file path (PEM)")
def proxy(
    config_path: str | None,
    host: str | None,
    port: int | None,
    upstream: str | None,
    transparent_mode: bool | None,
    reload_on_change: bool,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    ssl_keyfile_password: str | None = None,
    ssl_ca_certs: str | None = None,
) -> None:
    """Start the privacy proxy server."""
    from privyx.config.loader import load_config
    from privyx.core.errors import PrivyxError

    extra: dict[str, Any] = {}
    if host is not None:
        extra["host"] = host
    if port is not None:
        extra["port"] = port
    if upstream is not None:
        extra["upstream_url"] = upstream
        extra.setdefault("provider", {})["base_url"] = upstream
    if transparent_mode is not None:
        extra["proxy"] = {"mode": "transparent" if transparent_mode else "gateway"}
    tls_extra: dict[str, Any] = {}
    if ssl_certfile is not None:
        tls_extra["certfile"] = ssl_certfile
    if ssl_keyfile is not None:
        tls_extra["keyfile"] = ssl_keyfile
    if ssl_keyfile_password is not None:
        tls_extra["keyfile_password"] = ssl_keyfile_password
    if ssl_ca_certs is not None:
        tls_extra["ca_certs"] = ssl_ca_certs
    if tls_extra:
        extra["tls"] = tls_extra

    watch_path: Path | None = None
    if reload_on_change:
        resolved = config_path or os.environ.get("PRIVYX_CONFIG")
        if resolved:
            watch_path = Path(resolved)
        else:
            click.echo("--reload needs a config file; serving without it.", err=True)

    try:
        settings = load_config(config_path, extra=extra)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if (settings.tls.certfile and not settings.tls.keyfile) or (
        settings.tls.keyfile and not settings.tls.certfile
    ):
        click.echo(
            "Error: Both --ssl-certfile and --ssl-keyfile are required for HTTPS.",
            err=True,
        )
        sys.exit(1)

    while True:
        try:
            if not asyncio.run(_run_server(settings, watch_path)):
                return
            click.echo("Config changed \u2014 restarting\u2026")
        except PrivyxError as exc:
            # Without --reload this is fatal, as it always was; with it, a config
            # that parses but cannot be built must not kill the dev server.
            click.echo(f"Error: {exc}", err=True)
            if watch_path is None:
                sys.exit(1)
            _wait_for_change(watch_path, _mtime(watch_path))

        assert watch_path is not None  # only a watcher can ask for a restart
        while True:
            try:
                settings = load_config(config_path, extra=extra)
                break
            except PrivyxError as exc:
                # A half-saved YAML must not kill it either.
                click.echo(f"Error: {exc}", err=True)
                _wait_for_change(watch_path, _mtime(watch_path))


def _mtime(path: Path) -> int | None:
    """Modification time, or ``None`` while the file is momentarily missing."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _wait_for_change(path: Path, last: int | None) -> None:
    # ponytail: mtime poll, 1s — swap for watchfiles if it ever matters
    while True:
        time.sleep(1.0)
        now = _mtime(path)
        if now is not None and now != last:
            return


async def _watch_config(path: Path, event: asyncio.Event) -> None:
    """Set ``event`` once the config file changes on disk."""
    last = _mtime(path)
    while True:
        await asyncio.sleep(1.0)
        now = _mtime(path)
        if now is not None and now != last:
            event.set()
            return


async def _serve_until_reload(server: Any, event: asyncio.Event | None) -> None:
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


async def _run_server(settings: Any, watch_path: Path | None = None) -> bool:
    """Build the engine from settings, serve the selected app, and release resources.

    Returns ``True`` when the server stopped because the config file changed.
    """
    from privyx.core.builder import build_audit_logger, build_engine
    from privyx.observability.logging import configure_logging
    from privyx.plugins.loader import load_plugins

    configure_logging(settings)
    # Load plugins before building the engine so a plugin detector/operator/etc.
    # is registered by the time build_engine resolves the configured types.
    hooks = load_plugins(settings)
    audit = build_audit_logger(settings)
    engine, close_vault = await build_engine(settings, audit=audit)
    await hooks.run_startup()
    reload_event = asyncio.Event() if watch_path is not None else None
    watcher = (
        asyncio.create_task(_watch_config(watch_path, reload_event))
        if watch_path is not None and reload_event is not None
        else None
    )
    try:
        if settings.proxy.mode == "transparent":
            await _serve_transparent(settings, engine, audit, reload_event)
        else:
            await _serve_gateway(settings, engine, audit, reload_event)
    finally:
        if watcher is not None:
            watcher.cancel()
        await hooks.run_shutdown()
        await close_vault()
        audit.close()
    return reload_event is not None and reload_event.is_set()


def build_transparent_proxy(settings: Any, engine: Any, audit: Any) -> Any:
    """The transparent proxy ``settings`` describe; ``privyx run`` serves it too."""
    from privyx.providers.registry import resolve_api_key, resolve_origin
    from privyx.proxy.transparent import TransparentProxy

    return TransparentProxy(
        engine,
        origin=resolve_origin(settings),
        routes=dict(settings.proxy.routes),
        passthrough_unknown=settings.proxy.passthrough_unknown,
        forward_client_auth=settings.proxy.forward_client_auth,
        api_key=resolve_api_key(settings),
        extra_headers=dict(settings.provider.headers),
        timeout=settings.proxy.timeout,
        connect_timeout=settings.proxy.connect_timeout,
        max_connections=settings.proxy.max_connections,
        audit=audit,
        session_strategy=settings.session.strategy,
    )


async def _serve_transparent(
    settings: Any, engine: Any, audit: Any, reload_event: asyncio.Event | None = None
) -> None:
    """Serve the drop-in transparent reverse proxy."""
    import uvicorn

    from privyx.config.redact import redact
    from privyx.gateway.transparent import create_transparent_app
    from privyx.providers.registry import resolve_origin

    origin = resolve_origin(settings)
    proxy_instance = build_transparent_proxy(settings, engine, audit)

    scheme = "https" if settings.is_tls else "http"
    routes = ", ".join(f"{path}→{schema}" for path, schema in settings.proxy.routes.items())
    click.echo(f"Privyx transparent proxy listening on {scheme}://{settings.host}:{settings.port}")
    click.echo(f"Upstream origin: {redact(origin, 'url')}")
    click.echo(f"Routes: {routes}")
    click.echo(
        f"Engine: detector={settings.detector_type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type} "
        f"session={settings.session.strategy}"
    )

    app = create_transparent_app(engine, proxy_instance, settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
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
    try:
        await _serve_until_reload(server, reload_event)
    finally:
        await proxy_instance.close()


async def _serve_gateway(
    settings: Any, engine: Any, audit: Any, reload_event: asyncio.Event | None = None
) -> None:
    """Serve the gateway: our own endpoints, one fixed upstream endpoint."""
    import uvicorn

    from privyx.config.redact import redact
    from privyx.gateway.server import Gateway
    from privyx.providers.registry import build_provider, resolve_base_url

    provider = build_provider(settings)
    gateway = Gateway(engine=engine, provider=provider, settings=settings, audit=audit)

    scheme = "https" if settings.is_tls else "http"
    routes = ", ".join(f"{path}→{schema}" for path, schema in gateway.routes.items())
    click.echo(f"Privyx proxy listening on {scheme}://{settings.host}:{settings.port}")
    click.echo(f"Upstream: {redact(resolve_base_url(settings), 'url')}")
    click.echo(f"Routes: {routes}")
    click.echo(
        f"Engine: detector={settings.detector_type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type} "
        f"session={settings.session.strategy}"
    )

    server = uvicorn.Server(
        uvicorn.Config(
            gateway.app,
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
    try:
        await _serve_until_reload(server, reload_event)
    finally:
        await provider.close()
