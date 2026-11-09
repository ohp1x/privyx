"""`privyx scan` command — find what a workspace should keep from the model."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click

from privyx.cli.workspace import Found

#: The file ``privyx scan`` adds to, and how ``privyx.yaml`` names it.
TERMS_FILE = "privyx.d/terms.yaml"
_HEADER = "# Added by `privyx scan`, which only ever adds to this file: edit it freely.\n"


@click.command()
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Add everything found, without asking")
@click.option("--dry-run", is_flag=True, help="Show what was found and write nothing")
def scan(path: Path, yes: bool, dry_run: bool) -> None:
    """Find names and secrets in a workspace for Privyx to mask.

    Reads the git history (authors), the git remotes (owner and repository),
    the package manifests (project name), and the keys of .env files.  What you
    keep is added to privyx.d/terms.yaml, which privyx.yaml includes.  The
    values in .env files are never written: only a pattern that masks the value
    assigned to each key.
    """
    from privyx.cli import workspace
    from privyx.config.loader import is_trusted, trust
    from privyx.core.errors import PrivyxError

    root = path.resolve()
    terms_file = root / TERMS_FILE
    config_file = root / "privyx.yaml"
    try:
        existing = _read(terms_file)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    detector = existing.get("detector", {})
    terms = {e: {str(v).casefold() for v in vs} for e, vs in detector.get("terms", {}).items()}
    patterns = detector.get("patterns", {})

    found = workspace.unique(
        workspace.git_terms(root) + workspace.package_terms(root) + _env_keys(root)
    )
    new = [
        item
        for item in found
        if (
            item.entity not in patterns
            if _is_env(item)
            else item.value.casefold() not in terms.get(item.entity, ())
        )
    ]
    if not new:
        click.echo(f"Nothing new to add to {terms_file}.")
        return
    click.echo(f"Found in {root}:")
    width = max(len(item.entity) for item in new)
    for i, item in enumerate(new, 1):
        shown = f"value of {item.value}" if _is_env(item) else item.value
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

    trusted_before = config_file.is_file() and is_trusted(config_file)
    terms_existed = terms_file.exists()
    _add(existing, new)
    terms_file.parent.mkdir(parents=True, exist_ok=True)
    terms_file.write_text(_dump(existing), encoding="utf-8")
    click.echo(f"Added {len(new)} to {terms_file}")

    created = not config_file.exists()
    if created:
        config_file.write_text(f"include: [{TERMS_FILE}]\n", encoding="utf-8")
        click.echo(f"Created {config_file}, which includes it")
    else:
        _check_included(config_file, terms_file)
    try:
        # Trust only what was trusted before or is wholly written here: a
        # privyx.yaml that came with the project is the user's to review.
        if trusted_before or (created and not terms_existed):
            trust(config_file)
        elif not is_trusted(config_file):
            click.echo(f"Run `privyx trust` in {root} to use it.")
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc


def _env_keys(root: Path) -> list[Found]:
    """A pattern per ``.env`` key, unless the built-in detector already masks its value."""
    from privyx.cli import workspace
    from privyx.core.context import Context
    from privyx.privacy.detector.builtin import RegexDetector

    builtin = RegexDetector()
    found = []
    for key, value, name in workspace.env_values(root):
        entity = workspace.env_entity(key)
        assignment = f"{key}={value}"
        spans = builtin.detect_sync(assignment, Context()).spans
        if entity and not any(s.start <= len(key) + 1 and s.end >= len(assignment) for s in spans):
            found.append(Found(entity, key, name))
    return found


def env_pattern(key: str) -> str:
    """A regex masking the value assigned to ``key``: ``KEY=…``, ``KEY: …``, ``"KEY": "…"``.

    A quoted value is masked up to its closing quote, spaces and all.
    """
    return (
        rf"""(?<![\w.-]){key}["']?[ \t]*[=:][ \t]*(?P<q>["'`])?"""
        r"""(?P<value>(?(q)(?:\\.|(?!(?P=q))[^\r\n\\])+|[^\s"'`,;]+))"""
    )


def _is_env(item: Found) -> bool:
    return item.entity == f"ENV_{item.value}"


def _numbers(text: str, count: int) -> set[int]:
    numbers = set()
    for part in re.split(r"[\s,]+", text.strip()):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= count:
            raise click.BadParameter(f"{part!r} is not a number from 1 to {count}")
        numbers.add(int(part))
    return numbers


def _read(path: Path) -> dict[str, Any]:
    from privyx.config.loader import _read_yaml
    from privyx.core.errors import ConfigError

    if not path.exists():
        return {}
    data = _read_yaml(path)
    detector = data.get("detector", {})
    if not isinstance(detector, dict) or not all(
        isinstance(detector.get(key, {}), dict) for key in ("terms", "patterns")
    ):
        raise ConfigError(f"{path}: 'detector' must be a mapping with 'terms' and 'patterns'")
    return data


def _add(data: dict[str, Any], new: list[Found]) -> None:
    detector = data.setdefault("detector", {})
    for item in sorted(new, key=lambda item: (item.entity, item.value)):
        if _is_env(item):
            detector.setdefault("patterns", {})[item.entity] = env_pattern(item.value)
        else:
            detector.setdefault("terms", {}).setdefault(item.entity, []).append(item.value)


def _dump(data: dict[str, Any]) -> str:
    import yaml  # type: ignore[import-untyped]

    return _HEADER + str(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))


def _check_included(config_file: Path, terms_file: Path) -> None:
    """Say what to change when ``config_file`` does not bring in ``terms_file``'s terms."""
    from privyx.config.loader import _included, _read_yaml
    from privyx.core.errors import ConfigError

    try:
        data = _read_yaml(config_file)
        included = terms_file in [file.resolve() for file in _included(config_file, data)]
    except ConfigError:
        included = False
    if not included:
        click.echo(f"Add {TERMS_FILE} to the include list of {config_file} to use it.")
    elif isinstance(data.get("detector"), list):
        click.echo(
            f"{config_file} gives detector as a list, which replaces the terms it includes; "
            f"move the terms of {TERMS_FILE} into that list.",
            err=True,
        )
