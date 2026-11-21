"""`privyx proxy` command — starts the privacy proxy server."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import click

from privyx.cli.config import config_options


@click.command()
@config_options
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
    help="Restart the server when a config file changes (development).",
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
    from privyx.app import Privyx
    from privyx.config import active_config_paths
    from privyx.core import PrivyxError

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

    try:
        app = Privyx(config_path, **extra)
        settings = app.settings
        watch_paths = active_config_paths(config_path) if reload_on_change else []
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if reload_on_change and not watch_paths:
        click.echo("--reload needs a config file; serving without it.", err=True)

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
            if not app.serve(watch=watch_paths):
                return
            click.echo("Config changed \u2014 restarting\u2026")
        except PrivyxError as exc:
            # Without --reload this is fatal, as it always was; with it, a config
            # that parses but cannot be built must not kill the dev server.
            click.echo(f"Error: {exc}", err=True)
            if not watch_paths:
                sys.exit(1)
            _wait_for_change(watch_paths, _mtimes(watch_paths))

        assert watch_paths  # only a watcher can ask for a restart
        while True:
            try:
                app = Privyx(config_path, **extra)
                _ = app.settings
                break
            except PrivyxError as exc:
                # A half-saved YAML must not kill it either.
                click.echo(f"Error: {exc}", err=True)
                _wait_for_change(watch_paths, _mtimes(watch_paths))


def _mtimes(paths: list[Path]) -> tuple[int, ...] | None:
    """Modification times, or ``None`` while a file is momentarily missing."""
    try:
        return tuple(path.stat().st_mtime_ns for path in paths)
    except OSError:
        return None


def _wait_for_change(paths: list[Path], last: tuple[int, ...] | None) -> None:
    # ponytail: mtime poll, 1s — swap for watchfiles if it ever matters
    while True:
        time.sleep(1.0)
        now = _mtimes(paths)
        if now is not None and now != last:
            return
