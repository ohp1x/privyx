"""`privyx config` — show, change, check, and migrate the configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import click

from privyx.cli._options import config_options, deprecated, json_option


def _fail(exc: Exception) -> NoReturn:
    click.echo(f"Error: {exc}", err=True)
    sys.exit(1)


@click.group("config", invoke_without_command=True)
@click.option("--show", is_flag=True, hidden=True)
@click.option("--sources", "with_sources", is_flag=True, hidden=True)
@click.option("--path", "show_path", is_flag=True, hidden=True)
@click.option("--schema", "show_schema", is_flag=True, hidden=True)
@config_options
@click.pass_context
def config(
    ctx: click.Context, show: bool, with_sources: bool, show_path: bool, show_schema: bool
) -> None:
    """Show, change, check, and migrate the configuration.

    Without a command, a summary of the settings in use.
    """
    if ctx.invoked_subcommand is not None:
        return
    if show_schema:
        deprecated("`privyx config --schema`", "`privyx config schema`")
        ctx.invoke(schema)
    elif show or with_sources:
        deprecated("`privyx config --show` and `--sources`", "`privyx config show [--sources]`")
        ctx.invoke(show_cmd, with_sources=with_sources)
    elif show_path:
        deprecated("`privyx config --path`", "`privyx config path`")
        ctx.invoke(path)
    else:
        _summary()


def _summary() -> None:
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.providers import resolve_base_url
    from privyx.security import redact
    from privyx.version import __version__

    try:
        settings = Privyx().settings
    except PrivyxError as exc:
        _fail(exc)
    click.echo(f"Privyx {__version__}")
    click.echo(f"  Server: {settings.server.host}:{settings.server.port}")
    # The resolved URL, not the raw field: an empty base_url means the
    # type's default, and printing it blank would be misleading.
    click.echo(f"  Upstream: {redact(resolve_base_url(settings), 'url')}")
    click.echo(f"  Provider: {settings.provider.type}")
    click.echo(f"  Vault: {settings.vault.type}")
    click.echo(f"  Session: {settings.session.strategy}")
    click.echo(f"  Detectors: {settings.detector_type}")
    click.echo(f"  Policy: {settings.policy.type}")
    click.echo(f"  Operator: {settings.operator.type}")


@config.command("show")
@click.option(
    "--sources",
    "with_sources",
    is_flag=True,
    help="Pair each setting with the layer it comes from.",
)
def show_cmd(with_sources: bool) -> None:
    """Show the settings in use as JSON, secrets masked."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.security import redact

    try:
        loaded = Privyx().config.show()
    except PrivyxError as exc:
        _fail(exc)
    values = redact(loaded.settings.model_dump(mode="json"))
    shown = _with_sources(values, loaded.sources) if with_sources else values
    click.echo(json.dumps(shown, indent=2))


def _with_sources(
    values: dict[str, Any], origins: dict[str, str], prefix: str = ""
) -> dict[str, Any]:
    """Pair redacted fields with their winning layer; model-only defaults have no origin."""
    result: dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            result.update(_with_sources(value, origins, path + "."))
        else:
            result[path] = {"value": value, "source": origins.get(path, "defaults")}
    return result


@config.command("get")
@click.argument("key")
@json_option
def get(key: str, as_json: bool) -> None:
    """Show the value of KEY, such as server.port; secrets masked."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        value = Privyx().config.get(key, masked=True)
    except PrivyxError as exc:
        _fail(exc)
    click.echo(_shown(value, as_json))


def _shown(value: Any, as_json: bool) -> str:
    if as_json or isinstance(value, dict | list):
        return json.dumps(value, indent=2 if isinstance(value, dict | list) else None)
    return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


@config.command("explain")
@click.argument("key")
@json_option
def explain(key: str, as_json: bool) -> None:
    """Show KEY's value, where it comes from, its default, and its variables."""
    from dataclasses import asdict

    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        found = Privyx().config.explain(key)
    except PrivyxError as exc:
        _fail(exc)
    if as_json:
        click.echo(json.dumps(asdict(found), indent=2))
        return
    click.echo(f"{found.key} = {_shown(found.value, False)}")
    click.echo(f"  source:  {found.source}")
    click.echo(f"  default: {_shown(found.default, False)}")
    env = found.env or "none: write it in a config file"
    if found.aliases:
        env += f" (also {', '.join(found.aliases)})"
    click.echo(f"  env:     {env}")
    for where in found.written:
        click.echo(f"  written: {where}")


def _scope(project: bool, file: Path | None) -> dict[str, Any]:
    return {"scope": "project" if project else "user", "file": file}


_PROJECT = click.option(
    "--project", is_flag=True, help="Write .privyx/settings.yaml here, not ~/.privyx/settings.yaml."
)
_FILE = click.option(
    "--file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write this file instead (needed with --config); one written by hand is backed up.",
)


@config.command("set")
@click.argument("key")
@click.argument("value")
@_PROJECT
@_FILE
def set_cmd(key: str, value: str, project: bool, file: Path | None) -> None:
    """Set KEY to VALUE in the settings file Privyx manages.

    VALUE is read by the type of the setting: `8001` for a port is a number,
    `[a, b]` a list.  It applies from the next start.  Config files written
    by hand are not changed, the managed one wins over them, unless named
    with --file: that one loses its comments, and is backed up first.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        changed = Privyx().config.set(key, value, **_scope(project, file))
    except PrivyxError as exc:
        _fail(exc)
    click.echo(f"Set {changed.key} in {changed.file}")
    _report(changed)


@config.command("unset")
@click.argument("key")
@_PROJECT
@_FILE
def unset(key: str, project: bool, file: Path | None) -> None:
    """Remove KEY from the settings file Privyx manages."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        changed = Privyx().config.unset(key, **_scope(project, file))
    except PrivyxError as exc:
        _fail(exc)
    click.echo(f"Removed {changed.key} from {changed.file}")
    _report(changed)


def _report(changed: Any) -> None:
    if changed.backup is not None:
        click.echo(
            f"  {changed.file} was rewritten without its comments; it was kept as {changed.backup}",
            err=True,
        )
    for where in changed.below:
        click.echo(f"  {where} also writes {changed.key}; the value from here is used")
    for where in changed.above:
        click.echo(f"  {where} also writes {changed.key}, and its value is used")


@config.command("path")
def path() -> None:
    """Show the config files in use, lowest precedence first."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        files = Privyx().config.files()
    except PrivyxError as exc:
        _fail(exc)
    click.echo("\n".join(str(p) for p in files) or "defaults")


@config.command("schema")
def schema() -> None:
    """Print the JSON Schema of a config file, with the plugins' types."""
    from privyx.app import Privyx

    click.echo(json.dumps(Privyx().config.schema(), indent=2))


@config.command("validate")
@click.argument(
    "file", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def validate(file: Path | None) -> None:
    """Check the configuration, or FILE alone, and show its warnings."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        warnings = Privyx().config.validate(file)
    except PrivyxError as exc:
        _fail(exc)
    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)
    click.echo("valid")


@config.command("migrate")
@click.argument(
    "file", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--write", is_flag=True, help="Rewrite the file, keeping it as FILE.bak; comments are lost."
)
def migrate(file: Path | None, write: bool) -> None:
    """Print FILE, or each config file in use, with the keys renamed in 0.3.0.

    Old keys still work, with a warning, until 0.4.0.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    app = Privyx()
    try:
        files = [file] if file is not None else app.config.files()
        done = [app.config.migrate(f, write=write) for f in files]
    except PrivyxError as exc:
        _fail(exc)
    for migrated in done:
        if migrated.left:
            click.echo(
                f"{migrated.file}: left {', '.join(migrated.left)} as written: moving it could "
                "change what another operator type in the config uses; "
                "write the value where that type is picked",
                err=True,
            )
        if not migrated.renamed:
            if not migrated.left:
                click.echo(f"{migrated.file}: nothing to migrate", err=True)
            continue
        if migrated.backup is not None:
            click.echo(
                f"Rewrote {migrated.file}, without its comments; it was kept as {migrated.backup}",
                err=True,
            )
            continue
        if len(done) > 1:
            click.echo(f"# {migrated.file}")
        click.echo(migrated.text, nl=False)
    if not write and any(m.renamed for m in done):
        click.echo("Comments are not kept; --write rewrites the file, with a backup.", err=True)


@config.command("edit")
@click.option("--project", is_flag=True, help="Edit this directory's config file instead.")
def edit(project: bool) -> None:
    """Open your config file in $EDITOR, then check it."""
    import os

    from privyx.app import Privyx
    from privyx.config import find_local_configs, is_trusted, trust
    from privyx.core import PrivyxError

    named = os.environ.get("PRIVYX_CONFIG")
    if named:
        target = Path(named)
    elif project:
        local = [p for p in find_local_configs() if p.name != "settings.yaml"]
        target = local[-1] if local else Path("privyx.yaml")
    else:
        target = Path.home() / ".privyx" / "config.yaml"
    trusted = project and target.exists() and is_trusted(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    click.edit(filename=str(target))
    if trusted:
        trust(target)  # your own edit
    try:
        warnings = Privyx().config.validate()
    except PrivyxError as exc:
        _fail(exc)
    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)


@config.command("trust")
def trust() -> None:
    """Trust this directory's privyx.yaml and .privyx/ config files.

    A config file found in the working directory can load plugins and choose
    the upstream, so Privyx reads one only after you trust it here, and again
    after each change to it.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        paths = Privyx().config.trust()
    except PrivyxError as exc:
        _fail(exc)
    for trusted in paths:
        click.echo(f"Trusted {trusted}")


@config.command("lint")
def lint() -> None:
    """Show spellings of your terms and built-in values that are not masked.

    Each term in the config is tried in other spellings (DB_JOHN, JOHN__SMITH,
    john%20smith, zero-width and fullwidth characters), and a few fake built-in
    values inside identifiers (host_10.0.0.1), through the configured regex
    detectors and policy.  A detector option is suggested only when a retry
    with it masks the value.  Terms from another file that no detector uses
    are reported first.  Exits 1 when a spelling is not fully masked.
    """
    import asyncio

    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        found = asyncio.run(Privyx().diagnostics.lint())
    except PrivyxError as exc:
        _fail(exc)
    for warning in found.warnings:
        click.echo(f"warning: {warning}")
    for detector in found.unchecked:
        click.echo(f"Not checked: detector {detector}")
    if not found.checked:
        click.echo("No regex detector to check.")
        return
    for advice, missed in found.advice.items():
        click.echo(advice)
        for item in missed:
            click.echo(f"  {item.state:<7} {item.entity:<14} {item.text!r}")
    click.echo(
        f"Checked {_count(found.spellings, 'spelling')} of {_count(found.terms, 'term')} "
        f"and {_count(found.samples, 'built-in value')}: "
        + (f"{found.leaks} not fully masked." if found.leaks else "all masked.")
    )
    sys.exit(1 if found.leaks else 0)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' * (n != 1)}"


@config.command("routes")
def routes() -> None:
    """Show which request paths are masked, and with which wire schema."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.security import redact

    try:
        found = Privyx().config.routes()
    except PrivyxError as exc:
        _fail(exc)
    click.echo(f"Mode: {found.mode}  Upstream: {redact(found.upstream, 'url')}")
    click.echo("")
    width = max(len("PATH"), *(len(p) for p in found.routes)) if found.routes else len("PATH")
    click.echo(f"{'PATH':<{width}}  SCHEMA")
    for route, kind in found.routes.items():
        click.echo(f"{route:<{width}}  {kind}")
    click.echo("")
    click.echo(f"Other paths: {found.other}")


@click.command("trust", hidden=True)
@click.pass_context
def trust_alias(ctx: click.Context) -> None:
    """Trust this directory's config files (use `privyx config trust`)."""
    deprecated("`privyx trust`", "`privyx config trust`")
    ctx.invoke(trust)


@click.command("lint", hidden=True)
@config_options
@click.pass_context
def lint_alias(ctx: click.Context) -> None:
    """Show terms and values that are not masked (use `privyx config lint`)."""
    deprecated("`privyx lint`", "`privyx config lint`")
    ctx.invoke(lint)
