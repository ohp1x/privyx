"""`privyx config` command — inspect and show configuration."""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from privyx import __version__
from privyx.config.redact import redact


@click.command("config")
@click.option("--show", is_flag=True, help="Show current config as JSON (secrets masked)")
@click.option(
    "--sources", is_flag=True, help="Show each setting's value and source as JSON (secrets masked)"
)
@click.option("--path", is_flag=True, help="Show the config files in use")
@click.option("--schema", is_flag=True, help="Print the JSON Schema of a config file")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
def config_cmd(
    show: bool, sources: bool, path: bool, schema: bool, config_path: str | None
) -> None:
    """Inspect configuration."""
    if schema:
        from privyx.config.schema import json_schema

        click.echo(json.dumps(json_schema(), indent=2))
        return
    from privyx.config.loader import active_config_paths, load_config
    from privyx.core.errors import PrivyxError
    from privyx.providers.registry import resolve_base_url

    try:
        if path and not show and not sources:
            active = active_config_paths(config_path)
            click.echo("\n".join(str(p) for p in active) or "defaults")
            return
        origins: dict[str, str] = {}
        settings = load_config(config_path, sources=origins if sources else None)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if sources:
        values = redact(settings.model_dump(mode="json"))
        click.echo(json.dumps(_with_sources(values, origins), indent=2))
    elif show:
        click.echo(json.dumps(redact(settings.model_dump(mode="json")), indent=2))
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


def _with_sources(
    values: dict[str, Any], origins: dict[str, str], prefix: str = ""
) -> dict[str, Any]:
    """Pair redacted fields with their winning layer; model-only defaults have no origin."""
    result: dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            result.update(_with_sources(value, origins, path + "."))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for index, item in enumerate(value):
                result.update(_with_sources(item, origins, f"{path}.{index}."))
        else:
            result[path] = {"value": value, "source": origins.get(path, "defaults")}
    return result
