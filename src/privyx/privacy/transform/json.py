"""Utilities for transforming structured (JSON) payloads.

These helpers walk JSON documents and apply a callback to every string leaf,
which lets the privacy engine process request/response bodies without
hard-coding provider schemas.

A value may only say what it is with its key: ``hunter2`` is a secret in
``{"password": "hunter2"}``, and a context word may be the key.
:func:`with_key` writes a value as text with its key in front, and
:func:`value_spans` brings what is found there back onto the value (see
:meth:`~privyx.core.engine.PrivacyEngine.transform`).
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from privyx.core.result import Span

#: What ``json.dumps`` escapes in a string (``ensure_ascii=False``).
_ESCAPED = re.compile(r'["\\\x00-\x1f]')


async def map_values(
    value: Any,
    fn: Callable[[str], Awaitable[str]],
    under: Callable[[str, str], Awaitable[str]] | None = None,
    *,
    paths: list[str] | None = None,
) -> Any:
    """Rebuild ``value`` with ``fn`` applied to every string leaf, in order.

    ``under(key, text)``, when given, stands in for ``fn`` on a string under
    a key, also from inside a list (``{"passwords": ["..."]}``), to see it
    with that key.  With ``under``, numbers go to ``fn`` as text, alone: with
    its key, ``{"token_limit": 4000}`` would read as a secret.  Numbers are
    left alone without ``under``.

    Args:
        value: Any JSON-compatible value.
        fn: Applied to each string leaf.
        under: Applied to each string leaf under a key, with the key.
        paths: If given, only leaves whose JSON path starts with one of these
            (e.g. ``["$.messages", "$.items[*].note"]``) are changed.

    Returns:
        A new value with the leaves replaced.
    """

    async def walk(node: Any, path: str, owner: str | None) -> Any:
        if isinstance(node, list):
            return [await walk(item, f"{path}[*]", owner) for item in node]
        if isinstance(node, dict):
            return {key: await walk(item, f"{path}.{key}", key) for key, item in node.items()}
        if paths is not None and not any(path.startswith(p) for p in paths):
            return node
        text = leaf_text(node) if under is not None else node if isinstance(node, str) else None
        if text is None:
            return node
        if under is not None and owner is not None and isinstance(node, str):
            return put_back(node, text, await under(owner, text))
        return put_back(node, text, await fn(text))

    return await walk(value, "$", None)


def leaf_text(value: Any) -> str | None:
    """A string, or a number as text (a card number may come as one); else ``None``."""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def put_back(value: Any, text: str, changed: str) -> Any:
    """``changed`` in place of ``value``; a number stays one unless it was masked.

    A masked number becomes its token, a string, and is restored as a string.
    """
    return changed if isinstance(value, str) or changed != text else value


def with_key(key: str, value: str) -> str:
    """``value`` with its key in front, as text: ``key: "value"``, JSON-escaped."""
    return f"{key}: {json.dumps(value, ensure_ascii=False)}"


def value_spans(key: str, value: str, spans: list[Span]) -> list[Span]:
    """``spans`` found in ``with_key(key, value)``, cut to the value, at its offsets.

    A span ending inside an escape (``\\n`` for a newline) takes the character;
    one starting inside it, at the ``n``, starts after it: the detector read
    the escape as a letter of what follows (``nalice@example.com``).
    """
    if not spans:
        return []
    start = len(key) + 3
    end = start + len(json.dumps(value, ensure_ascii=False)) - 2
    # Each character JSON escaped: where its escape starts, how long it is,
    # and how many characters escapes added before it.
    at: list[int] = []
    size: list[int] = []
    before: list[int] = []
    added = 0
    for match in _ESCAPED.finditer(value):
        length = len(json.dumps(match.group(), ensure_ascii=False)) - 2
        at.append(match.start() + added)
        size.append(length)
        before.append(added)
        added += length - 1

    def raw(offset: int) -> int:
        k = bisect.bisect_right(at, offset) - 1
        if k < 0:
            return offset
        if offset < at[k] + size[k]:
            return at[k] - before[k]
        return offset - before[k] - size[k] + 1

    def begin(offset: int) -> int:
        k = bisect.bisect_right(at, offset) - 1
        if k >= 0 and at[k] < offset < at[k] + size[k]:
            return at[k] - before[k] + 1
        return raw(offset)

    out = []
    for span in spans:
        low, high = max(span.start, start) - start, min(span.end, end) - start
        if low < high:
            first, last = begin(low), raw(high - 1) + 1
            if first < last:
                out.append(Span(first, last, span.entity_type, value[first:last]))
    return out


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
