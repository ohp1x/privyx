"""`privyx scan` command — find what a workspace should keep from the model."""

from __future__ import annotations

import re
from pathlib import Path

import click


@click.command()
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Add everything found, without asking")
@click.option("--dry-run", is_flag=True, help="Show what was found and write nothing")
def scan(path: Path, yes: bool, dry_run: bool) -> None:
    """Find names and secrets in a workspace for Privyx to mask.

    Reads the git history (authors), the git remotes (owner and repository),
    the package manifests (project name), and the keys of .env files.  What you
    keep is added to privyx.d/terms.yaml, which privyx.yaml includes.  The
    values in .env files are never written: only a pattern that masks the
    value assigned to each key.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.workspace import TERMS_FILE, is_env

    workspace = Privyx().workspace
    try:
        found = workspace.scan(path)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    new = found.new
    if not new:
        click.echo(f"Nothing new to add to {found.terms_file}.")
        return
    click.echo(f"Found in {found.root}:")
    width = max(len(item.entity) for item in new)
    for i, item in enumerate(new, 1):
        shown = f"value of {item.value}" if is_env(item) else item.value
        click.echo(f"  {i:>3}  {item.entity:<{width}}  {shown}  ({item.source})")
    if dry_run:
        return
    if not yes:
        skip = click.prompt(
            "Numbers to leave out (e.g. 2,5), or Enter to keep all",
            default="",
            show_default=False,
            value_proc=lambda text: _numbers(text, len(new)),
        )
        new = [item for i, item in enumerate(new, 1) if i not in skip]
        if not new:
            click.echo("Nothing added.")
            return

    try:
        added = workspace.add(found, new)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Added {added.count} to {added.terms_file}")
    if added.created:
        click.echo(f"Created {found.config_file}, which includes it")
    elif added.include_error is not None:
        click.echo(f"{added.include_error}; fix it to use {TERMS_FILE}.", err=True)
    elif not added.included:
        click.echo(f"Add {TERMS_FILE} to the include list of {found.config_file} to use it.")
    elif added.detector_list:
        click.echo(
            f"{found.config_file} gives detector as a list, which replaces the terms it "
            f"includes; move the terms of {TERMS_FILE} into that list.",
            err=True,
        )
    if added.untrusted:
        click.echo(f"Run `privyx trust` in {found.root} to use it.")


def _numbers(text: str, count: int) -> set[int]:
    numbers = set()
    for part in re.split(r"[\s,]+", text.strip()):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= count:
            raise click.BadParameter(f"{part!r} is not a number from 1 to {count}")
        numbers.add(int(part))
    return numbers
