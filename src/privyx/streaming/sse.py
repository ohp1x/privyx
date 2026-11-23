"""SSE stream adapter.

SSE envelope parsing and re-serialization, decoupled from text
transformation.  The privacy engine only ever sees the ``data`` payload.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass


@dataclass(slots=True)
class SSEEvent:
    """A single server-sent event."""

    data: str
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    #: Lines forwarded as they came, in an event with no data: a block's
    #: comments, and the ``id`` and ``retry`` of a block without data.  Only
    #: from a decoder asked for them (:class:`SSEDecoder`).
    comment: str | None = None

    def controls(self) -> list[str]:
        """The ``id`` and ``retry`` lines; an empty ``id`` resets the client's."""
        lines: list[str] = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        return lines

    def to_str(self) -> str:
        if self.comment is not None:
            return f"{self.comment}\n\n"
        lines: list[str] = []
        if self.event:
            lines.append(f"event: {self.event}")
        lines += self.controls()
        for data_line in self.data.split("\n"):
            lines.append(f"data: {data_line}")
        return "\n".join(lines) + "\n\n"


def parse_sse(chunk: str) -> list[SSEEvent]:
    """Parse a self-contained block of SSE text into events.

    Only complete events (terminated by a blank line) are returned; a trailing
    partial block is discarded.  For a stream arriving in arbitrary pieces use
    :class:`SSEDecoder`, which carries the remainder across calls.
    """
    return SSEDecoder().feed(chunk)


class SSEDecoder:
    """Incremental SSE decoder that tolerates any input framing.

    Upstream transports differ in what they hand us: ``httpx.aiter_lines()``
    yields newline-stripped *lines*, ``aiter_bytes()`` yields arbitrary
    *chunks* that may split an event anywhere.  Feeding either to a stateless
    parser drops events — a single line never contains the ``\\n\\n``
    terminator, so nothing is ever emitted.

    This decoder buffers across calls and emits an event only once its
    terminating blank line has arrived.  With ``comments``, a block's comment
    lines (a keep-alive) come first, as an event of their own, with the
    ``id`` and ``retry`` of a block that has no data.

    Example::

        decoder = SSEDecoder()
        for line in ("data: hi", ""):
            for event in decoder.feed_line(line):
                print(event.data)
    """

    def __init__(self, *, comments: bool = False) -> None:
        self._comments = comments
        self._buffer = ""
        # The last chunk ended in CR, so an LF opening the next one is its pair.
        self._after_cr = False

    def feed(self, chunk: str) -> list[SSEEvent]:
        """Feed a raw chunk; return every event completed by it."""
        if not chunk:
            return []
        if self._after_cr and chunk.startswith("\n"):
            chunk = chunk[1:]
        self._after_cr = chunk.endswith("\r")
        self._buffer += chunk
        return self._drain()

    def feed_line(self, line: str) -> list[SSEEvent]:
        """Feed one newline-stripped line (as ``httpx.aiter_lines()`` yields).

        A blank line terminates the current event.
        """
        self._buffer += line.rstrip("\r") + "\n"
        self._after_cr = False
        return self._drain()

    def _drain(self) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        # Normalize CRLF/CR so the blank-line split works on any transport.
        # A CRLF split across two chunks is joined in feed(), which drops the LF.
        self._buffer = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n" in self._buffer:
            block, _, self._buffer = self._buffer.partition("\n\n")
            events += self._block(block)
        return events

    def flush(self) -> list[SSEEvent]:
        """Emit any final event left unterminated at end of stream."""
        remainder, self._buffer = self._buffer.strip("\n"), ""
        return self._block(remainder) if remainder else []

    def _block(self, block: str) -> list[SSEEvent]:
        """The events of one block: the lines kept as they came, if asked for, and its event."""
        events: list[SSEEvent] = []
        event = _parse_block(block)
        if self._comments:
            kept = [
                line
                for line in block.split("\n")
                if line.startswith(":")
                or (event is None and line.partition(":")[0] in ("id", "retry"))
            ]
            if kept:
                events.append(SSEEvent(data="", comment="\n".join(kept)))
        if event is not None:
            events.append(event)
        return events


def _parse_block(block: str) -> SSEEvent | None:
    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None
    retry: int | None = None
    for line in block.split("\n"):
        if line.startswith(":"):
            continue  # comment
        if ":" in line:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
        else:
            field, value = line, ""
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        elif field == "id" and "\0" not in value:  # a client ignores one with a NUL
            event_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            # Digits only, as a client reads it: `+5` must not become `5`.
            with contextlib.suppress(ValueError):  # more digits than int() converts
                retry = int(value)
    if not data_lines:
        return None
    return SSEEvent(
        data="\n".join(data_lines),
        event=event_name,
        id=event_id,
        retry=retry,
    )
