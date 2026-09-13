"""Frontier — holds a partial pseudonym that may span chunk boundaries.

The frontier is the stateful piece that lets the streaming deanonymizer
recognize ``<PRIVYX_EMA`` when the remainder ``IL_5>`` arrives in the next
chunk.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Frontier:
    """State for an in-progress (possibly partial) pseudonym match.

    Attributes:
        pending: Text buffered since the last flush that may be a prefix of a
            pseudonym.
        is_pending: Whether the buffered text is currently being matched.
    """

    pending: str = ""
    is_pending: bool = False

    def reset(self) -> None:
        self.pending = ""
        self.is_pending = False

    def feed(self, char: str) -> None:
        self.pending += char
        self.is_pending = True

    def flush(self) -> str:
        """Return and clear the pending buffer."""
        flushed = self.pending
        self.reset()
        return flushed