"""Utilities for transforming structured (JSON) payloads.

These helpers walk JSON documents and apply a callback to every string leaf,
which lets the privacy engine process request/response bodies without
hard-coding provider schemas.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def map_strings(
    value: Any,
    transform: Callable[[str], str],
    *,
    paths: list[str] | None = None,
    _path: str = "$",
) -> Any:
    """Recursively apply ``transform`` to every string leaf.

    Args:
        value: Any JSON-compatible value.
        transform: Callable applied to each string leaf.
        paths: If given, only transform leaves whose JSON path matches
            one of these prefixes (e.g. ``["$.messages", "$.content"]``).
        _path: Internal path tracking (used for recursion).

    Returns:
        A new value with transformed strings.
    """
    if isinstance(value, str):
        if paths is None or any(_path.startswith(p) for p in paths):
            return transform(value)
        return value
    if isinstance(value, list):
        return [map_strings(item, transform, paths=paths, _path=f"{_path}[*]") for item in value]
    if isinstance(value, dict):
        return {
            key: map_strings(v, transform, paths=paths, _path=f"{_path}.{key}")
            for key, v in value.items()
        }
    return value


def walk_paths(value: Any) -> list[tuple[str, Any]]:
    """Return a flat list of ``(json_path, value)`` for every node."""
    results: list[tuple[str, Any]] = []

    def _walk(node: Any, path: str) -> None:
        results.append((path, node))
        if isinstance(node, dict):
            for key, child in node.items():
                _walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                _walk(child, f"{path}[{index}]")

    _walk(value, "$")
    return results
