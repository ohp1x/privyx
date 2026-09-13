"""Structured data transforms (dataclasses, pydantic models, CSV rows, ...)."""

from __future__ import annotations

from typing import Any


def transform_mapping(
    mapping: dict[str, Any],
    transform: Any,
    *,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Apply ``transform`` to selected fields of a dict.

    Args:
        mapping: The dict to transform.
        transform: A callable ``(value: str) -> str``.
        fields: Fields to transform.  If None, all string values.
    """
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if fields is None or key in fields:
            if isinstance(value, str):
                result[key] = transform(value)
                continue
        result[key] = value
    return result