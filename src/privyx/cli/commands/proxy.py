"""`privyx proxy` command — starts the privacy proxy server."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
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
def proxy(
    config_path: str | None,
    host: str | None,
    port: int | None,
    upstream: str | None,
    transparent_mode: bool | None,
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

    try:
        settings = load_config(config_path, extra=extra)
        asyncio.run(_run_server(settings))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


async def _run_server(settings: Any) -> None:
    """Build the engine from settings, serve the selected app, and release resources."""
    from privyx.core.builder import build_engine

    engine, close_vault = await build_engine(settings)
    try:
        if settings.proxy.mode == "transparent":
            await _serve_transparent(settings, engine)
        else:
            await _serve_gateway(settings, engine)
    finally:
        await close_vault()


async def _serve_transparent(settings: Any, engine: Any) -> None:
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
        await server.serve()
    finally:
        await proxy_instance.close()


async def _serve_gateway(settings: Any, engine: Any) -> None:
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
    gateway = Gateway(engine=engine, proxy=proxy_instance, settings=settings)
    server = uvicorn.Server(
        uvicorn.Config(
            gateway.app, host=settings.host, port=settings.port, log_level=settings.log_level
        )
    )
    try:
        await server.serve()
    finally:
        await provider.close()
