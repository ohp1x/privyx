"""`privyx proxy` command — starts the privacy proxy server."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import click


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
def proxy(
    config_path: str | None,
    host: str | None,
    port: int | None,
    upstream: str | None,
    transparent_mode: bool | None,
    reload_on_change: bool,
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

    if importlib.util.find_spec("uvicorn") is None:  # pragma: no cover
        click.echo(
            "The proxy server requires privyx[server]. Install with: pip install privyx[server]",
            err=True,
        )
        sys.exit(1)

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
    if event is None:
        await server.serve()
        return
    serving = asyncio.create_task(server.serve())
    waiting = asyncio.create_task(event.wait())
    await asyncio.wait({serving, waiting}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True  # graceful uvicorn shutdown
    waiting.cancel()
    await serving


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


async def _serve_transparent(
    settings: Any, engine: Any, audit: Any, reload_event: asyncio.Event | None = None
) -> None:
    """Serve the drop-in transparent reverse proxy."""
    import uvicorn

    from privyx.gateway.transparent import create_transparent_app
    from privyx.providers.registry import resolve_origin
    from privyx.proxy.transparent import TransparentProxy

    origin = resolve_origin(settings)
    proxy_instance = TransparentProxy(
        engine,
        origin=origin,
        routes=dict(settings.proxy.routes),
        forward_client_auth=settings.proxy.forward_client_auth,
        api_key=settings.provider.api_key or None,
        extra_headers=dict(settings.provider.headers),
        audit=audit,
    )

    routes = ", ".join(f"{path}→{schema}" for path, schema in settings.proxy.routes.items())
    click.echo(f"Privyx transparent proxy listening on http://{settings.host}:{settings.port}")
    click.echo(f"Upstream origin: {origin}")
    click.echo(f"Routes: {routes}")
    click.echo(
        f"Engine: detector={settings.detector.type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type}"
    )

    app = create_transparent_app(engine, proxy_instance, settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host=settings.host, port=settings.port, log_level=settings.log_level
        )
    )
    try:
        await _serve_until_reload(server, reload_event)
    finally:
        await proxy_instance.close()


async def _serve_gateway(
    settings: Any, engine: Any, audit: Any, reload_event: asyncio.Event | None = None
) -> None:
    """Serve the narrow chat-only gateway."""
    import uvicorn

    from privyx.gateway.server import Gateway
    from privyx.providers.registry import build_provider, resolve_base_url
    from privyx.proxy.http import HTTPProxy
    from privyx.streaming.adapters.registry import build_stream_adapter

    provider = build_provider(settings)
    adapter = build_stream_adapter(settings.provider.type)

    click.echo(f"Privyx proxy listening on http://{settings.host}:{settings.port}")
    click.echo(f"Upstream: {resolve_base_url(settings)}")
    click.echo(
        f"Engine: detector={settings.detector.type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type}"
    )

    proxy_instance = HTTPProxy(engine=engine, provider=provider, stream_adapter=adapter)
    gateway = Gateway(engine=engine, proxy=proxy_instance, settings=settings, audit=audit)
    server = uvicorn.Server(
        uvicorn.Config(
            gateway.app, host=settings.host, port=settings.port, log_level=settings.log_level
        )
    )
    try:
        await _serve_until_reload(server, reload_event)
    finally:
        await provider.close()
