"""ID generation utilities."""

from __future__ import annotations

import uuid


def generate_id(prefix: str = "", length: int = 16) -> str:
    """Generate a random ID with an optional prefix.

    Args:
        prefix: Optional prefix string (e.g. ``"ses"``, ``"req"``).
        length: Number of hex characters from the UUID (max 32).

    Returns:
        A string like ``"ses_3f2a1b4c8e9d0f12"`` or ``"3f2a1b4c8e9d0f12"``.
    """
    hex_id = uuid.uuid4().hex[:length]
    return f"{prefix}_{hex_id}" if prefix else hex_id


def request_id() -> str:
    """Generate a short request-scoped ID."""
    return generate_id("req", 12)


def trace_id() -> str:
    """Generate a trace ID (full UUID hex, no prefix)."""
    return uuid.uuid4().hex
