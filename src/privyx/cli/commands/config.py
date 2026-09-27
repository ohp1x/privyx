"""`privyx config` command — inspect and show configuration."""

from __future__ import annotations

import json

import click

from privyx import __version__
from privyx.config.redact import redact


@click.command("config")
@click.option("--show", is_flag=True, help="Show current config as JSON (secrets masked)")
@click.option("--path", is_flag=True, help="Show config file path")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
def config_cmd(show: bool, path: bool, config_path: str | None) -> None:
    """Inspect configuration."""
    import os

    from privyx.config.loader import load_config
    from privyx.providers.registry import resolve_base_url

    settings = load_config(config_path)
    if show:
        click.echo(json.dumps(redact(settings.model_dump(mode="json")), indent=2))
    elif path:
        cfg_path = config_path or os.environ.get("PRIVYX_CONFIG", "defaults")
        click.echo(cfg_path)
    else:
        click.echo(f"Privyx {__version__}")
        click.echo(f"  Host: {settings.host}:{settings.port}")
        # The resolved URL, not the raw field: `upstream_url` is empty unless
        # explicitly overridden, and printing it blank would be misleading.
        click.echo(f"  Upstream: {redact(resolve_base_url(settings), 'url')}")
        click.echo(f"  Provider: {settings.provider.type}")
        click.echo(f"  Vault: {settings.vault.type}")
        click.echo(f"  Session: {settings.session.strategy}")
        click.echo(f"  Detector: {settings.detector_type}")
        click.echo(f"  Policy: {settings.policy.type}")
        click.echo(f"  Operator: {settings.operator.type}")
