"""`privyx install-hook` and `privyx hook` — keep secrets and placeholders out of git."""

from __future__ import annotations

import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from privyx.token.codec import TokenCodec

_MARKER = "# Installed by `privyx install-hook`."
_HOOKS = {
    "pre-commit": "exec privyx hook pre-commit\n",
    "commit-msg": 'exec privyx hook commit-msg "$1"\n',
}
#: Built-in entities that are credentials, not personal data: code and tests
#: are full of emails and phone numbers, but a key must never be committed.
_SECRETS = ("API_KEY", "PRIVATE_KEY", "JWT", "AUTH_TOKEN", "URL_CREDENTIAL", "SECRET")


@click.command("install-hook")
def install_hook() -> None:
    """Install git hooks that stop a commit holding a secret or a Privyx placeholder.

    pre-commit checks the staged changes, commit-msg the commit message.  A
    hook that is already there is left alone: add the `privyx hook` line it
    names to that hook instead.
    """
    hooks = _git("rev-parse", "--git-path", "hooks").strip()
    hooks_dir = Path(hooks)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    refused = []
    for name, line in _HOOKS.items():
        path = hooks_dir / name
        script = f"#!/bin/sh\n{_MARKER}\n{line}"
        if path.exists() and _MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            refused.append(f"{path} exists; add this line to it: {line.removeprefix('exec ')}")
            continue
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        click.echo(f"Installed {path}")
    for message in refused:
        click.echo(f"Error: {message}", err=True)
    if refused:
        sys.exit(1)


@click.group()
def hook() -> None:
    """The checks the hooks of `privyx install-hook` run."""


@hook.command("pre-commit")
def pre_commit() -> None:
    """Fail when the staged changes add a secret or a Privyx placeholder."""
    diff = _git(
        # What is committed, not a view of it: no textconv driver, and a file
        # marked binary (`-diff`) shown as text rather than skipped.
        "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
        "--src-prefix=a/",
        "--dst-prefix=b/", "-U0", "--diff-filter=d",
    )  # fmt: skip
    codec = _codec()
    found = [
        f"{path}:{line}  {what}"
        for path, lines in _added_lines(diff).items()
        for line, what in _findings(lines, codec)
    ]
    _report(found)


@hook.command("commit-msg")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def commit_msg(file: Path) -> None:
    """Fail when the commit message in FILE holds a secret or a Privyx placeholder.

    The whole file is checked, # lines too: `git commit -m` keeps them, and a
    hook cannot tell which cleanup mode the commit uses.
    """
    text = file.read_text(encoding="utf-8", errors="replace")
    lines = list(enumerate(text.split("\n"), 1))
    _report([f"commit message:{line}  {what}" for line, what in _findings(lines, _codec())])


def _findings(lines: list[tuple[int, str]], codec: TokenCodec) -> list[tuple[int, str]]:
    """``(line number, what)`` for each secret and placeholder in ``lines``.

    The lines are checked as one text, so a private key spanning several is
    found too.  A secret is named by its type only, so the hook's output, which
    a coding agent may read, never repeats it.
    """
    from privyx.core.context import Context
    from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector

    text = "\n".join(line for _, line in lines)
    starts = [0]
    for _, line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)

    def number(offset: int) -> int:
        return lines[bisect_right(starts, offset) - 1][0]

    detector = RegexDetector({entity: DEFAULT_PATTERNS[entity] for entity in _SECRETS})
    found = [
        (number(span.start), span.entity_type)
        for span in detector.detect_sync(text, Context()).merged(text).spans
    ]
    found += [(number(match.start), f"placeholder {match.text}") for match in codec.finditer(text)]
    return sorted(found)


def _added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """The lines a ``git diff -U0`` adds, with their line numbers, by file."""
    files: dict[str, list[tuple[int, str]]] = {}
    path = None
    number = 0
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            in_hunk, path = False, None
        elif not in_hunk and line.startswith("+++ "):
            # A name git still quotes (one with a quote or control character)
            # keeps its escapes: it only labels the finding.
            name = line[4:].strip('"')
            path = name[2:] if name.startswith("b/") else None
        elif line.startswith("@@"):
            in_hunk = True
            start = re.search(r"\+(\d+)", line)
            number = int(start[1]) if start else 0
        elif in_hunk and line.startswith("+") and path is not None:
            files.setdefault(path, []).append((number, line[1:]))
            number += 1
    return files


def _codec() -> TokenCodec:
    """The configured token codec, or the default one when the config cannot be read."""
    from privyx.config.loader import load_config
    from privyx.core.builder import build_codec
    from privyx.core.errors import PrivyxError
    from privyx.token.codec import FormatCodec

    try:
        return build_codec(load_config())
    except PrivyxError as exc:
        click.echo(f"privyx: using the default token format: {exc}", err=True)
        return FormatCodec.default()


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


def _git(*args: str) -> str:
    try:
        # Bytes, decoded here: text mode would also split lines at a lone
        # carriage return, and what follows one in an added line would lose
        # its `+` and go unchecked.
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], capture_output=True, check=False
        )
    except OSError as exc:
        raise click.ClickException(f"cannot run git: {exc.strerror or exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise click.ClickException(stderr or f"git {args[0]} failed")
    return result.stdout.decode("utf-8", errors="replace")
