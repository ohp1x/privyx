"""``Exchange``: one request through the privacy pipeline, without transport types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Exchange:
    """One request through the privacy pipeline, as the adapter that received it read it.

    The pipeline fills in the rest as it goes: the dialect of the route, the
    session, and, on a masking route, the body parsed as JSON.
    """

    method: str
    #: The path, percent-decoded, from its leading ``/``.
    path: str
    headers: Mapping[str, str]
    #: The bytes, or their chunks as they arrive; the parsed JSON on a masking route.
    body: Any = b""
    #: The query parameters in order, a repeated name kept.
    query: list[tuple[str, str]] = field(default_factory=list)
    #: The path as sent, before percent-decoding, when the server has it.
    raw_path: str | None = None
    #: The API format of the route (``openai``, ``anthropic``, ...); ``None``
    #: outside the masking routes.
    dialect: str | None = None
    session_id: str | None = None
