"""What a workspace tells about its people, owners, and secrets.

``privyx scan`` writes what it finds to a config file, after asking.  It reads
the git history and remotes, the package manifests, and ``.env`` files.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_DOUBLE = re.compile(r'"((?:\\.|[^"\\])*)"')
_SINGLE = re.compile(r"'((?:\\'|[^'])*)'")
#: The escapes dotenv decodes; any other backslash is kept as written.
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_EMAIL = re.compile(r"\s*<[^>]*>\s*$")
#: ``.env`` files that hold examples rather than the workspace's own values.
_EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist")


@dataclass(frozen=True)
class Found:
    """A value to mask, the entity it is masked as, and where it was found."""

    entity: str
    value: str
    source: str


def git_terms(root: Path, max_commits: int | None = None) -> list[Found]:
    """Authors and co-authors of the git history, and the owner and name of each remote."""
    found = []
    limit = [f"--max-count={max_commits}"] if max_commits else []
    log = _git(
        root, "log", "--all", *limit, "--format=%aN%n%(trailers:key=Co-authored-by,valueonly)"
    )
    for line in log.splitlines():
        name = _EMAIL.sub("", line).strip()
        if name and not name.endswith("[bot]"):
            found.append(Found("PERSON", name, "git history"))
    for line in _git(root, "config", "--get-regexp", r"^remote\..*\.url$").splitlines():
        key, _, url = line.partition(" ")
        remote = key.removeprefix("remote.").removesuffix(".url")
        found += _owner_and_name(_url_path(url), f"git remote {remote}")
    return found


def package_terms(root: Path) -> list[Found]:
    """The project names in ``pyproject.toml``, ``package.json``, ``Cargo.toml``, and ``go.mod``."""
    found = []
    for manifest, read in (
        ("pyproject.toml", lambda t: _toml(t, "project") or _toml(t, "tool", "poetry")),
        ("Cargo.toml", lambda t: _toml(t, "package")),
        ("package.json", _json_name),
    ):
        try:
            name = read((root / manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(name, str):
            continue
        scope, _, base = name.rpartition("/")  # npm's @scope/name
        if scope.removeprefix("@"):
            found.append(Found("ORGANIZATION", scope.removeprefix("@"), manifest))
        found.append(Found("PROJECT", base, manifest))
    try:
        module = re.search(r"(?m)^module\s+(\S+)", (root / "go.mod").read_text(encoding="utf-8"))
    except OSError:
        module = None
    if module:
        found += _owner_and_name(urlsplit("https://" + module[1]).path, "go.mod")
    return found


def env_values(root: Path) -> list[tuple[str, str, str]]:
    """``(key, value, file name)`` for each value in the workspace's ``.env`` files
    that is worth masking: not empty, a number, or a plain word like ``true``.

    Read in memory only; callers must not write the values anywhere.
    """
    found = []
    for path in sorted(root.glob(".env*")):
        if not path.is_file() or path.name.endswith(_EXAMPLE_SUFFIXES):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _ASSIGNMENT.fullmatch(line)
            if not match:
                continue
            value = _value(match[2].strip())
            if len(value) >= 6 and not value.isalpha() and re.search(r"[A-Za-z]", value):
                found.append((match[1], value, path.name))
    return found


def _value(raw: str) -> str:
    """The value a ``.env`` line assigns, as dotenv reads it.

    Quoted, up to the unescaped closing quote, so a comment after it is
    dropped.  In double quotes ``\\\\``, ``\\'``, ``\\"`` and the control
    characters ``\\a \\b \\f \\n \\r \\t \\v`` are decoded; in single quotes only
    ``\\\\`` and ``\\'``.  Any other backslash stays.  Unquoted, up to a `` #``
    comment.
    """
    double = _DOUBLE.match(raw)
    if double:
        return re.sub(r"\\([\\'\"abfnrtv])", lambda m: _ESCAPES.get(m[1], m[1]), double[1])
    single = _SINGLE.match(raw)
    if single:
        return re.sub(r"\\([\\'])", r"\1", single[1])
    return raw.split(" #", 1)[0].strip()


def env_entity(key: str) -> str | None:
    """The entity an ``.env`` key's value is masked as, or ``None`` if a token cannot name it."""
    return f"ENV_{key}" if len(key) <= 60 else None


def unique(found: list[Found]) -> list[Found]:
    """``found`` without repeats, which terms match regardless of case; sources are joined."""
    seen: dict[tuple[str, str], Found] = {}
    for item in found:
        if len(item.value) < 3:
            continue  # a word that short would be masked inside everything
        key = (item.entity, item.value.casefold())
        first = seen.get(key)
        if first is None:
            seen[key] = item
        elif item.source not in first.source.split(", "):
            seen[key] = Found(first.entity, first.value, f"{first.source}, {item.source}")
    return list(seen.values())


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""  # no git
    return result.stdout if result.returncode == 0 else ""


def _url_path(url: str) -> str:
    """The path of a git remote URL, never its host or credentials; empty for a local path."""
    if "://" in url:
        parts = urlsplit(url)
        return parts.path if parts.hostname and parts.scheme != "file" else ""
    host, sep, path = url.partition(":")  # scp-like: git@host:owner/name.git
    return path if sep and len(host) > 1 and "/" not in host else ""


def _owner_and_name(path: str, source: str) -> list[Found]:
    parts = [p for p in path.strip("/").removesuffix(".git").split("/") if p]
    if len(parts) < 2:
        return []
    return [Found("ORGANIZATION", parts[0], source), Found("PROJECT", parts[-1], source)]


def _toml(text: str, *keys: str) -> object:
    table: object = tomllib.loads(text)
    for key in keys:
        table = table.get(key) if isinstance(table, dict) else None
    return table.get("name") if isinstance(table, dict) else None


def _json_name(text: str) -> object:
    data = json.loads(text)
    return data.get("name") if isinstance(data, dict) else None
