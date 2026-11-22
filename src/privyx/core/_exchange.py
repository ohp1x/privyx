"""``Exchange``: one request through the privacy pipeline, without transport types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # types only: core imports no other Privyx package at run time
    from privyx.core._session import Session
    from privyx.dialects import Dialect


@dataclass(slots=True)
class Exchange:
    """One request through the privacy pipeline, as the adapter that received it read it.

    The pipeline fills in the rest as it goes: the dialect of the route, the
    session, what correlates its records, and, on a masking route, the body
    parsed as JSON.
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
    #: The API format of the route; ``None`` outside the masking routes.
    dialect: Dialect | None = None
    session_id: str | None = None
    #: The session, when it lasts this one exchange and no vault holds it.
    ephemeral: Session | None = None
    #: The id in each audit record of this exchange.
    request_id: str = ""
    #: When it began, a ``time.perf_counter()`` mark.
    started: float = 0.0
    #: Its tracing span, when tracing is on.
    span: Any = None
