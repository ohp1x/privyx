"""SSE stream adapter.

SSE envelope parsing and re-serialization, decoupled from text
transformation.  The privacy engine only ever sees the ``data`` payload.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SSEEvent:
    """A single server-sent event."""

    data: str
    event: str | None = None
    id: str | None = None
    retry: int | None = None

    def to_str(self) -> str:
        lines: list[str] = []
        if self.event:
            lines.append(f"event: {self.event}")
        if self.id:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        for data_line in self.data.split("\n"):
            lines.append(f"data: {data_line}")
        return "\n".join(lines) + "\n\n"


def parse_sse(chunk: str) -> list[SSEEvent]:
    """Parse a self-contained block of SSE text into events.

    Only complete events (terminated by a blank line) are returned; a trailing
    partial block is discarded.  For a stream arriving in arbitrary pieces use
    :class:`SSEDecoder`, which carries the remainder across calls.
    """
    events: list[SSEEvent] = []
    # SSE events are separated by a blank line.  We split conservatively.
    blocks = chunk.split("\n\n")
    # The last block may be incomplete; if the chunk ends with "\n\n" it is
    # complete.  We don't consume partial trailing blocks.
    for block in blocks[:-1]:
        event = _parse_block(block)
        if event is not None:
            events.append(event)
    return events


class SSEDecoder:
    """Incremental SSE decoder that tolerates any input framing.

    Upstream transports differ in what they hand us: ``httpx.aiter_lines()``
    yields newline-stripped *lines*, ``aiter_bytes()`` yields arbitrary
    *chunks* that may split an event anywhere.  Feeding either to a stateless
    parser drops events — a single line never contains the ``\\n\\n``
    terminator, so nothing is ever emitted.

    This decoder buffers across calls and emits an event only once its
    terminating blank line has arrived.

    Example::

        decoder = SSEDecoder()
        for line in ("data: hi", ""):
            for event in decoder.feed_line(line):
                print(event.data)
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[SSEEvent]:
        """Feed a raw chunk; return every event completed by it."""
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain()

    def feed_line(self, line: str) -> list[SSEEvent]:
        """Feed one newline-stripped line (as ``httpx.aiter_lines()`` yields).

        A blank line terminates the current event.
        """
        self._buffer += line.rstrip("\r") + "\n"
        return self._drain()

    def _drain(self) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        # Normalize CRLF/CR so the blank-line split works on any transport.
        self._buffer = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n" in self._buffer:
            block, _, self._buffer = self._buffer.partition("\n\n")
            event = _parse_block(block)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[SSEEvent]:
        """Emit any final event left unterminated at end of stream."""
        remainder, self._buffer = self._buffer.strip("\n"), ""
        if not remainder:
            return []
        event = _parse_block(remainder)
        return [event] if event is not None else []

    @property
    def pending(self) -> str:
        """Buffered text not yet forming a complete event."""
        return self._buffer


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
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None
    if not data_lines:
        return None
    return SSEEvent(
        data="\n".join(data_lines),
        event=event_name,
        id=event_id,
        retry=retry,
    )