"""`privyx hook` — git hooks that keep secrets and placeholders out of git."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from privyx.cli._options import deprecated

if TYPE_CHECKING:
    from privyx.app import Privyx


@click.group()
def hook() -> None:
    """Git hooks that stop a commit holding a secret or a Privyx placeholder."""


@hook.command("install")
def install() -> None:
    """Install the git hooks in this repository.

    pre-commit checks the staged changes, commit-msg the commit message.  A
    hook that is already there is left alone: add the `privyx hook` line it
    names to that hook instead.
    """
    from privyx.core import PrivyxError

    try:
        installed = _app().workspace.install_hooks()
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    for path in installed.written:
        click.echo(f"Installed {path}")
    for path, line in installed.refused:
        click.echo(f"Error: {path} exists; add this line to it: {line}", err=True)
    if installed.refused:
        sys.exit(1)


@click.command("install-hook", hidden=True)
@click.pass_context
def install_hook(ctx: click.Context) -> None:
    """Install the git hooks (use `privyx hook install`)."""
    deprecated("`privyx install-hook`", "`privyx hook install`")
    ctx.invoke(install)


@hook.command("pre-commit")
def pre_commit() -> None:
    """Fail when the staged changes add a secret or a Privyx placeholder."""
    from privyx.core import PrivyxError

    workspace = _app().workspace
    _warn_codec(workspace.codec()[1])
    try:
        found = workspace.check_staged()
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    _report([f"{f.path}:{f.line}  {f.what}" for f in found])


@hook.command("commit-msg")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def commit_msg(file: Path) -> None:
    """Fail when the commit message in FILE holds a secret or a Privyx placeholder.

    The whole file is checked, # lines too: `git commit -m` keeps them, and a
    hook cannot tell which cleanup mode the commit uses.
    """
    text = file.read_text(encoding="utf-8", errors="replace")
    workspace = _app().workspace
    _warn_codec(workspace.codec()[1])
    _report([f"{f.path}:{f.line}  {f.what}" for f in workspace.check_message(text)])


def _app() -> Privyx:
    from privyx.app import Privyx

    return Privyx()


def _warn_codec(error: str | None) -> None:
    if error is not None:
        click.echo(f"privyx: using the default token format: {error}", err=True)


def _report(found: list[str]) -> None:
    if not found:
        return
    click.echo("privyx: this commit holds values that must not leave this machine:", err=True)
    for item in found:
        click.echo(f"  {item}", err=True)
    click.echo(
        "Remove the secrets, restore placeholders with `privyx unmask`, "
        "or commit with --no-verify to skip this check.",
        err=True,
    )
    sys.exit(1)
