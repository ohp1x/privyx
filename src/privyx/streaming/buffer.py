"""Reusable byte/text buffer for splitting streamed content safely."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TextBuffer:
    """Accumulates text across chunks without corrupting boundaries.

    Useful when the upstream sends content split mid-token, mid-pseudonym, or
    mid-multibyte-character.
    """

    parts: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(p) for p in self.parts)

    def append(self, text: str) -> None:
        self.parts.append(text)

    def drain(self) -> str:
        """Concatenate and clear the buffer."""
        out = "".join(self.parts)
        self.parts.clear()
        return out
