"""Git hooks that keep secrets and Privyx placeholders out of a repository.

``pre-commit`` checks the lines the staged changes add, ``commit-msg`` the
commit message; ``privyx mcp serve`` checks a diff it is given the same way.
A secret is named by its type only, so what a hook prints, which a coding agent
may read, never repeats it.
"""

from __future__ import annotations

import functools
import re
import subprocess
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from privyx.core import PrivyxError

if TYPE_CHECKING:
    from privyx.detectors import YamlDetector
    from privyx.token import FormatCodec

_MARKER = "# Installed by `privyx install-hook`."
_HOOKS = {
    "pre-commit": "exec privyx hook pre-commit\n",
    "commit-msg": 'exec privyx hook commit-msg "$1"\n',
}


@dataclass(frozen=True)
class Installed:
    """What :func:`install_hooks` did: the hooks written, and the ones left alone."""

    written: list[Path]
    #: An existing hook of another's, and the line to add to it.
    refused: list[tuple[Path, str]]


def install_hooks() -> Installed:
    """Install the hooks in the repository of the working directory.

    A hook that is already there and not Privyx's is left alone.

    Raises:
        PrivyxError: If git cannot be run or finds no repository.
    """
    hooks_dir = Path(git("rev-parse", "--git-path", "hooks").strip())
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    refused: list[tuple[Path, str]] = []
    for name, line in _HOOKS.items():
        path = hooks_dir / name
        if path.exists() and _MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            refused.append((path, line.removeprefix("exec ")))
            continue
        path.write_text(f"#!/bin/sh\n{_MARKER}\n{line}", encoding="utf-8")
        path.chmod(0o755)
        written.append(path)
    return Installed(written, refused)


def staged_diff() -> str:
    """The changes staged for the next commit, as a unified diff without context.

    Raises:
        PrivyxError: If git cannot be run or finds no repository.
    """
    return git(
        # What is committed, not a view of it: no textconv driver, and a file
        # marked binary (`-diff`) shown as text rather than skipped.
        "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
        "--src-prefix=a/",
        "--dst-prefix=b/", "-U0", "--diff-filter=d",
    )  # fmt: skip


def findings(lines: list[tuple[int, str]], codec: FormatCodec) -> list[tuple[int, str]]:
    """``(line number, what)`` for each secret and placeholder in ``lines``.

    The lines are checked as one text, so a private key spanning several is
    found too.  A secret is named by its type only, so the hook's output, which
    a coding agent may read, never repeats it.
    """
    from privyx.core import Context

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
def _secret_detector() -> YamlDetector:
    """Built once: `privyx mcp serve` checks a diff per file, on every call."""
    from privyx.core import SECRET_TYPES
    from privyx.detectors import YamlDetector
    from privyx.detectors._pattern import DEFAULT_PATTERNS

    # Code and tests are full of emails and phone numbers, but a key must
    # never be committed.
    return YamlDetector(
        patterns={entity: DEFAULT_PATTERNS[entity] for entity in SECRET_TYPES},
        underscore_separates_words=False,
    )


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
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
    git_side = old.startswith("a/") or old == "/dev/null"
    return name.removeprefix("b/") if git_side else name


def git(*args: str) -> str:
    """The output of git ``args`` in the working directory.

    Raises:
        PrivyxError: If git cannot be run, or fails.
    """
    try:
        # Bytes, decoded here: text mode would also split lines at a lone
        # carriage return, and what follows one in an added line would lose
        # its `+` and go unchecked.
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], capture_output=True, check=False
        )
    except OSError as exc:
        raise PrivyxError(f"cannot run git: {exc.strerror or exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PrivyxError(stderr or f"git {args[0]} failed")
    return result.stdout.decode("utf-8", errors="replace")
