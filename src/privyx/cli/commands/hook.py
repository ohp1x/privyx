"""`privyx install-hook` and `privyx hook` — keep secrets and placeholders out of git."""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from privyx.privacy.detector.builtin import RegexDetector
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

    text = "\n".join(line for _, line in lines)
    starts = [0]
    for _, line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)

    def number(offset: int) -> int:
        return lines[bisect_right(starts, offset) - 1][0]

    found = [
        (number(span.start), span.entity_type)
        for span in _secret_detector().detect_sync(text, Context()).merged(text).spans
    ]
    found += [(number(match.start), f"placeholder {match.text}") for match in codec.finditer(text)]
    return sorted(found)


@functools.cache
def _secret_detector() -> RegexDetector:
    """Built once: `privyx mcp serve` checks a diff per file, on every call."""
    from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector

    return RegexDetector({entity: DEFAULT_PATTERNS[entity] for entity in _SECRETS})


def _added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """The lines a unified diff adds, with their line numbers, by file.

    A hunk ends after the lines its header counts, so the next file's ``---`` /
    ``+++`` header is not read as content, with or without ``diff --git``
    lines.  Inside a hunk an empty line is context that lost its space.  A
    wrong count or a missing header must not hide a secret: every added line
    is kept, under ``(unknown)`` when no file names it, and a pair of
    ``---`` / ``+++`` lines outside a hunk is a header only when a hunk
    follows.  A combined diff (a merge: ``@@@``, one column per parent) adds
    the lines with a ``+`` and no ``-`` in their columns.
    """
    files: dict[str, list[tuple[int, str]]] = {}
    path = _UNKNOWN
    number = old = new = 0
    width = 1  # columns before a line's content
    lines = diff.split("\n")
    for i, line in enumerate(lines):
        if line.startswith((*_FILE_STARTS, "@@")):
            if line.startswith("@@") and (old or new) and _header(lines, i - 1):
                # The previous hunk's count was wrong and it took the next
                # header as content (kept, so nothing went unchecked); that
                # header still names the file.  Not when the count matched:
                # then `-- x` / `++ y` were a removed and an added line.
                path = _name(lines[i - 1], lines[i - 2])
            old = new = 0  # never hunk content, whatever the count said
        if (old > 0 or new > 0) and width > 1:
            # Combined: only the result's lines are counted.
            columns = line[:width]
            if "+" in columns and "-" not in columns:
                files.setdefault(path, []).append((number, line[width:]))
                number += 1
                new -= 1
            elif "-" not in columns and not line.startswith("\\"):
                number += 1
                new -= 1
        elif old > 0 or new > 0:
            if line.startswith("+"):
                files.setdefault(path, []).append((number, line[1:]))
                number += 1
                new -= 1
            elif line.startswith("-"):
                old -= 1
            elif not line.startswith("\\"):
                number += 1
                old -= 1
                new -= 1
        elif line.startswith(_FILE_STARTS):
            path = _UNKNOWN
        elif _header(lines, i) and lines[i + 1 : i + 2] and lines[i + 1].startswith("@@"):
            path = _name(line, lines[i - 1])
        elif combined := re.match(r"(@{3,}) (?:-\d+(?:,\d+)? )+\+(\d+)(?:,(\d+))? @", line):
            width = len(combined[1]) - 1
            number = int(combined[2])
            new = int(combined[3] or 1)
        elif line.startswith("@@"):
            width = 1
            counts = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if counts:
                old = int(counts[1] or 1)
                number = int(counts[2])
                new = int(counts[3] or 1)
            else:
                start = re.search(r"\+(\d+)", line)
                number = int(start[1]) if start else 0
        elif line.startswith("+"):
            files.setdefault(path, []).append((number, line[1:]))
            number += 1
    return files


#: The label of added lines no file header names.
_UNKNOWN = "(unknown)"

#: Lines that start a file in a git diff, ordinary or combined.
_FILE_STARTS = ("diff --git ", "diff --cc ", "diff --combined ")


def _header(lines: list[str], i: int) -> bool:
    """Whether ``lines[i]`` is a ``+++`` line right after a ``---`` line."""
    return i > 0 and lines[i].startswith("+++ ") and lines[i - 1].startswith("--- ")


def _name(line: str, previous: str) -> str:
    """The file a ``+++`` line names; ``previous`` is the ``---`` line before it.

    A name git still quotes (one with a quote or control character) keeps its
    escapes: it only labels the finding.  What follows a tab is the timestamp
    plain ``diff -u`` adds, and a CR is left by a diff pasted with CRLF.  The
    ``b/`` prefix is git's only when the old side has git's ``a/`` or is
    ``/dev/null``.
    """

    def clean(header: str) -> str:
        return header[4:].split("\t", 1)[0].rstrip("\r").strip('"')

    name, old = clean(line), clean(previous)
    if name == "/dev/null":
        return _UNKNOWN
    git = old.startswith("a/") or old == "/dev/null"
    return name.removeprefix("b/") if git else name


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
