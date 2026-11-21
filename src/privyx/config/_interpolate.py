"""``${NAME}`` references in config values, and the ones a profile or preset defers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from privyx.core import ConfigError

#: ``$${`` (a literal ``${``), ``${NAME}``, ``${NAME:-default}``, or any other
#: ``${`` -- the last one a malformed reference.
_REFERENCE = re.compile(r"\$\$\{|\$\{(?:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\})?")


#: Profile settings resolved only for the profile being run, so a reference in
#: another profile neither fails nor prompts for every command.  Presets wait
#: in the same way until one is applied.
_DEFERRED = ("env", "args", "privyx")


class _Deferred(str):
    """A reference in a profile's deferred settings, kept as written in ``file``."""

    file: Path

    def __new__(cls, text: str, file: Path) -> _Deferred:
        value = super().__new__(cls, text)
        value.file = file
        return value


def _settle(value: Any, *, expand: bool) -> Any:
    """Each :class:`_Deferred` in ``value`` interpolated, or as plain text without ``expand``."""
    if isinstance(value, dict):
        return {key: _settle(item, expand=expand) for key, item in value.items()}
    if isinstance(value, list):
        return [_settle(item, expand=expand) for item in value]
    if isinstance(value, _Deferred):
        return _interpolate(str(value), value.file) if expand else str(value)
    return value


def _interpolate(value: Any, path: Path, keys: tuple[Any, ...] = ()) -> Any:
    """Replace ``${NAME}`` and ``${NAME:-default}`` in every string value with the environment.

    ``${NAME:-default}`` takes the default when ``NAME`` is unset or empty, as in
    a shell.  An unset ``${NAME}`` with no default is an error: a missing key
    would otherwise reach the upstream as an empty string.  ``$${`` writes a
    literal ``${``.  Keys are left as written.  A profile's ``env``, ``args``,
    and ``privyx`` values are deferred (:class:`_Deferred`) until that profile
    runs, and a preset's until it is applied.
    """
    if isinstance(value, dict):
        return {key: _interpolate(item, path, (*keys, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, path, keys) for item in value]
    if not isinstance(value, str) or "${" not in value:
        return value
    if keys[:1] == ("presets",) or (
        keys[:1] == ("agents",) and len(keys) > 2 and keys[2] in _DEFERRED
    ):
        return _Deferred(value, path)

    def substitute(match: re.Match[str]) -> str:
        if match.group(0) == "$${":
            return "${"
        name, default = match.group(1), match.group(2)
        if name is None:
            raise ConfigError(
                f"malformed variable reference in {path}: use ${{NAME}} or "
                "${NAME:-default}, or $${ for a literal ${"
            )
        found = os.environ.get(name)
        if default is not None:
            return found or default
        if found is None:
            raise ConfigError(f"{path} uses ${{{name}}}, but {name} is not set")
        return found

    return _REFERENCE.sub(substitute, value)
