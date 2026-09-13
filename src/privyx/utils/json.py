"""JSON serialization helpers."""

from __future__ import annotations

import json
from typing import Any


def safe_dumps(value: Any, *, default: Any = None, **kwargs: Any) -> str:
    """Serialize ``value`` to a JSON string.

    Args:
        value: Any JSON-serializable value.
        default: Fallback serializer for non-serializable objects
            (passed to :func:`json.dumps`).
        **kwargs: Additional keyword arguments forwarded to :func:`json.dumps`.

    Returns:
        JSON string, or ``"null"`` if serialization fails.
    """
    try:
        return json.dumps(value, default=default, **kwargs)
    except (TypeError, ValueError):
        return "null"


def safe_loads(text: str, *, default: Any = None) -> Any:
    """Parse a JSON string, returning ``default`` on failure.

    Args:
        text: JSON-encoded string.
        default: Value returned when parsing fails (default ``None``).
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def compact_dumps(value: Any) -> str:
    """Serialize to compact JSON (no extra whitespace)."""
    return json.dumps(value, separators=(",", ":"))


def pretty_dumps(value: Any, indent: int = 2) -> str:
    """Serialize to pretty-printed JSON."""
    return json.dumps(value, indent=indent)
