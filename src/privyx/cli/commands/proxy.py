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
def proxy(
    config_path: str | None,
    host: str | None,
    port: int | None,
    upstream: str | None,
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
    """Build the engine from settings, serve, and release resources on exit."""
    import uvicorn

    from privyx.core.builder import build_engine
    from privyx.gateway.server import Gateway
    from privyx.providers.registry import build_provider, resolve_base_url
    from privyx.proxy.http import HTTPProxy
    from privyx.streaming.adapters.registry import build_stream_adapter

    engine, close_vault = await build_engine(settings)
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
    config = uvicorn.Config(
        gateway.app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await close_vault()
        await provider.close()
