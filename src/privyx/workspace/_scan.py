"""``privyx scan``: what a workspace should keep from the model, added to its config.

What is kept goes to ``privyx.d/terms.yaml``, which ``privyx.yaml`` includes.
The values in ``.env`` files are never written: only a pattern that masks the
value assigned to each key.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from privyx.core import ConfigError
from privyx.workspace._harvest import Found, env_entity, env_values, git_terms, package_terms
from privyx.workspace._harvest import unique as _unique

#: The file ``privyx scan`` adds to, and how ``privyx.yaml`` names it.
TERMS_FILE = "privyx.d/terms.yaml"
_HEADER = "# Added by `privyx scan`, which only ever adds to this file: edit it freely.\n"


@dataclass(frozen=True)
class Scan:
    """What :func:`scan` found in a workspace that its terms file does not hold yet."""

    root: Path
    terms_file: Path
    config_file: Path
    #: The terms file as it is, to add to.
    existing: dict[str, Any]
    new: list[Found]


@dataclass(frozen=True)
class Added:
    """What :func:`add` wrote, and what the workspace's config still needs."""

    terms_file: Path
    count: int
    #: ``privyx.yaml`` did not exist, and was written to include the terms file.
    created: bool
    #: Why ``privyx.yaml`` could not be read to see that it includes the file.
    include_error: str | None = None
    included: bool = True
    #: ``privyx.yaml`` includes the terms but keeps them from a detector that uses them.
    terms_unused: bool = False
    #: ``privyx.yaml`` was trusted with its new content.
    trusted: bool = False
    #: ``privyx.yaml`` is not trusted, so the terms are not used until it is.
    untrusted: bool = False


def scan(root: Path) -> Scan:
    """The names, owners, projects, and ``.env`` keys of ``root`` not in its terms file yet.

    Raises:
        ConfigError: If the terms file cannot be read.
    """
    root = root.resolve()
    terms_file = root / TERMS_FILE
    existing = _read(terms_file)
    section = _section(existing)
    terms = {e: {str(v).casefold() for v in vs} for e, vs in section.get("terms", {}).items()}
    patterns = section.get("patterns", {})
    found = _unique(git_terms(root) + package_terms(root) + env_keys(root))
    new = [
        item
        for item in found
        if (
            item.entity not in patterns
            if is_env(item)
            else item.value.casefold() not in terms.get(item.entity, ())
        )
    ]
    return Scan(root, terms_file, root / "privyx.yaml", existing, new)


def add(found: Scan, items: list[Found]) -> Added:
    """Add ``items`` of ``found`` to its terms file, and include that file.

    ``privyx.yaml`` is created to include it when there is none.  It is trusted
    only when it was trusted before or is wholly written here: one that came
    with the project is the user's to review.

    Raises:
        ConfigError: If ``privyx.yaml`` cannot be trusted.
    """
    from privyx.config import is_trusted, trust

    config_file, terms_file = found.config_file, found.terms_file
    trusted_before = config_file.is_file() and is_trusted(config_file)
    terms_existed = terms_file.exists()
    existing = found.existing
    _add(existing, items)
    terms_file.parent.mkdir(parents=True, exist_ok=True)
    terms_file.write_text(_dump(existing), encoding="utf-8")
    created = not config_file.exists()
    added = Added(terms_file, len(items), created)
    if created:
        config_file.write_text(f"include: [{TERMS_FILE}]\n", encoding="utf-8")
    else:
        added = _included(added, config_file, terms_file)
    if trusted_before or (created and not terms_existed):
        trust(config_file)
        return replace(added, trusted=True)
    return replace(added, untrusted=not is_trusted(config_file))


def env_keys(root: Path) -> list[Found]:
    """A pattern per ``.env`` key, unless the built-in detector already masks its value."""
    from privyx.core import Context
    from privyx.detectors import RegexDetector

    builtin = RegexDetector(underscore_separates_words=False)
    found = []
    for key, value, name in env_values(root):
        entity = env_entity(key)
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


def is_env(item: Found) -> bool:
    """Whether ``item`` is a ``.env`` key, whose value is masked by a pattern."""
    return item.entity == f"ENV_{item.value}"


def _read(path: Path) -> dict[str, Any]:
    from privyx.config import read_yaml

    if not path.exists():
        return {}
    data = read_yaml(path)
    section = _section(data)
    if not isinstance(section, dict) or not all(
        isinstance(section.get(key, {}), dict) for key in ("terms", "patterns")
    ):
        name = "detector" if "detector" in data else "detectors.regex"
        raise ConfigError(f"{path}: {name!r} must be a mapping with 'terms' and 'patterns'")
    return data


def _section(data: dict[str, Any]) -> Any:
    """Where a terms file holds its terms; one of an earlier version keeps its old key."""
    if "detector" in data:
        return data["detector"]
    detectors = data.get("detectors", {})
    return detectors.get("regex", {}) if isinstance(detectors, dict) else None


def _add(data: dict[str, Any], new: list[Found]) -> None:
    section = (
        data["detector"]
        if "detector" in data
        else data.setdefault("detectors", {}).setdefault("regex", {})
    )
    for item in sorted(new, key=lambda item: (item.entity, item.value)):
        if is_env(item):
            section.setdefault("patterns", {})[item.entity] = env_pattern(item.value)
        else:
            section.setdefault("terms", {}).setdefault(item.entity, []).append(item.value)


def _dump(data: dict[str, Any]) -> str:
    import yaml  # type: ignore[import-untyped]

    return _HEADER + str(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))


def _included(added: Added, config_file: Path, terms_file: Path) -> Added:
    """``added``, with whether ``config_file`` brings in ``terms_file``'s terms."""
    from privyx.config import included_files, read_yaml

    try:
        data = read_yaml(config_file)
        included = terms_file in [file.resolve() for file in included_files(config_file, data)]
    except ConfigError as exc:
        return replace(added, include_error=str(exc))
    return replace(added, included=included, terms_unused=included and _drops_terms(data))


def _drops_terms(data: dict[str, Any]) -> bool:
    """Whether a config including the terms file keeps them from the detector they are for."""
    if isinstance(data.get("detector"), list):
        return True  # a list replaces what is below it
    detectors = data.get("detectors")
    regex = detectors.get("regex") if isinstance(detectors, dict) else None
    return regex is False or (isinstance(regex, dict) and regex.get("enabled") is False)
