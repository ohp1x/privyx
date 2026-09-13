"""Events emitted by the streaming pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StreamEventType(StrEnum):
    DELTA = "delta"
    START = "start"
    END = "end"
    ERROR = "error"
    FLUSH = "flush"


@dataclass(slots=True)
class StreamEvent:
    """A unit of streaming output.

    Attributes:
        type: Event type.
        data: The transformed text delta (for DELTA events).
        raw: The raw chunk as received from upstream (optional).
        session_id: Session associated with this stream, if any.
        metadata: Additional event metadata.
    """

    type: StreamEventType
    data: str = ""
    raw: object | None = None
    session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "data": self.data,
            "session_id": self.session_id,
            "metadata": self.metadata,
        }
