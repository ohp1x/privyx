"""Restore a streamed reply: SSE events or JSON lines, whatever their chunking.

:class:`StreamRouter` turns a stream of raw upstream chunks into a stream of
restored SSE frames.  It is the one place that reconciles three buffers whose
boundaries never line up — bytes (a UTF-8 char may split across chunks), SSE
events (an event may split across chunks), and tokens (a pseudonym may split
across events) — and keeps held-back text in order: a trailing fragment that
might still grow into a token is flushed *before* any pass-through event and
at the end of the stream, never after.

The base restores plain SSE, whose data is text.  An API format reads its
events as JSON, which a dialect's subclass does (:meth:`_event`): only deltas
need per-event care, since a token may split across them; every other event
carries complete strings, so its JSON is restored leaf by leaf with the same
walk as a batch body and re-serialized only when something changed.

The class is deliberately synchronous: token resolution uses the session's
pseudonym map captured once at stream start (the response only ever references
tokens minted while transforming the matching request), so no ``await`` is
needed per event.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from privyx.core import walk_sync
from privyx.streaming.deanonymizer import (
    StreamDeanonymizer,
    StreamingDeanonymizer,
    TokenStreamProcessor,
)
from privyx.streaming.sse import SSEDecoder, SSEEvent
from privyx.token.codec import FormatCodec

if TYPE_CHECKING:
    from privyx.operators import Operator

#: The OpenAI stream's end, which is not JSON: matched as a raw sentinel.
DONE = "[DONE]"


class ObjectStream(Protocol):
    """Restores a stream whose events are whole JSON objects (Gemini's, Ollama's)."""

    def feed(self, payload: Any) -> list[Any]:
        """The objects to send for ``payload``: none while text is held back."""

    def flush(self) -> list[Any]:
        """The objects holding the text still held back."""


class StreamRouter:
    """Rewrites an upstream SSE stream, restoring the text of its events.

    Args:
        codec: Token codec (syntax authority) shared with the engine.
        resolve: Maps a token's text to its original value, or ``None`` when the
            token is unknown to this session (then it is passed through verbatim).
        make_processor: Factory for the per-field deanonymizer.  Defaults to a
            codec :class:`TokenStreamProcessor` (recognizes ``<PRIVYX_…>`` tokens
            by syntax).  A literal-restore operator (faker) supplies a factory
            building a trie-based :class:`StreamingDeanonymizer` instead — see
            :func:`select_processor_factory`.  The hold-back scan loop is
            identical either way; only the "is there a token here?" decision
            changes.
    """

    def __init__(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
    ) -> None:
        self._codec = codec
        self._resolve = resolve
        self._make_processor = make_processor or (lambda: TokenStreamProcessor(codec, resolve))

        self._decoder = SSEDecoder(comments=True)
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")

        self._text = self._make_processor()
        # Reused for complete strings: feed + flush always leaves it empty.
        self._full = self._make_processor()
        # The event a flushed tail of visible text is re-wrapped in.
        self._text_template: SSEEvent | None = None
        # Terminal events, sent after everything held back.
        self._pending_terminal: list[str] = []

    # -- public API --------------------------------------------------------

    def feed(self, chunk: bytes | str) -> list[str]:
        """Feed one raw upstream chunk; return zero or more frames to forward."""
        text = self._utf8.decode(chunk) if isinstance(chunk, (bytes, bytearray)) else chunk
        out: list[str] = []
        for event in self._decoder.feed(text):
            out.extend(self._handle(event))
        return out

    def flush(self) -> list[str]:
        """Drain every buffer at end of stream, in the correct order."""
        out: list[str] = []
        # Any event left unterminated by the upstream, plus the incremental
        # decoder's residue.
        for event in self._decoder.feed(self._utf8.decode(b"", final=True)) + self._decoder.flush():
            out.extend(self._handle(event))
        out.extend(self._flush_text_tail())
        out.extend(self._flush_held())
        out.extend(self._pending_terminal)
        self._pending_terminal = []
        return out

    # -- what a format changes ---------------------------------------------

    def _event(self, event: SSEEvent) -> list[str]:
        """The frames for one event: here its data is text, restored as it arrives."""
        delta = event.data
        if not delta:
            return self._passthrough(event)
        self._text_template = event
        out = self._text.feed(delta)
        return [self._wrap_text(out, event)] if out else []

    def _wrap_text(self, text: str, template: SSEEvent) -> str:
        """``text`` as a frame like ``template``, the latest event of visible text."""
        return replace(template, data=text).to_str()

    def _flush_text_tail(self) -> list[str]:
        """Held-back visible text, as frames."""
        tail = self._text.flush()
        if tail and self._text_template is not None:
            return [self._wrap_text(tail, self._text_template)]
        return []

    def _flush_held(self) -> list[str]:
        """Whatever else the format holds back at the end, such as buffered tool calls."""
        return []

    # -- shared ------------------------------------------------------------

    def _handle(self, event: SSEEvent) -> list[str]:
        if event.comment is not None:
            # A keep-alive may come in the middle of a token: held-back text stays held.
            return [event.to_str()]
        if event.data.strip() == DONE:
            self._hold_terminal(event)
            frames: list[str] = []
        else:
            frames = self._event(event)
        # An event held back, or rebuilt without them, still moves the client's id.
        unsent = [
            line
            for line in event.controls()
            if not any(line in frame.split("\n") for frame in frames)
        ]
        if unsent:
            frames = [*frames, SSEEvent(data="", comment="\n".join(unsent)).to_str()]
        # A format keeps the event as a template: later frames must not repeat them.
        event.id = event.retry = None
        return frames

    def _hold_terminal(self, event: SSEEvent) -> None:
        """Keep ``event`` for the end, without its id and retry, which go out now."""
        self._pending_terminal.append(replace(event, id=None, retry=None).to_str())

    def _passthrough(self, event: SSEEvent, *, drain: bool = True) -> list[str]:
        """Forward an event that is not restored delta by delta.

        Its complete strings are restored in place (:meth:`_restore_event`).
        ``drain`` flushes any held-back text first so ordering is preserved; it
        is disabled for control events (e.g. Anthropic's ``ping``) that may
        arrive *between* the chunks of a single token, where flushing would
        split it.
        """
        out = self._flush_text_tail() if drain else []
        out.append(self._restore_event(event).to_str())
        return out

    def _restore_full(self, text: str) -> str:
        """Deanonymize a complete string with the same recognition as text."""
        return self._full.feed(text) + self._full.flush()

    def _restore_arguments(self, text: str) -> str:
        """Deanonymize complete tool-call arguments, JSON-aware when they parse."""
        restored: str = walk_sync(text, self._restore_full, "arguments")
        return restored

    def _restore_event(self, event: SSEEvent) -> SSEEvent:
        """``event`` with every string leaf of its JSON restored.

        Re-serialized only when something changed, so an event without a token
        is forwarded byte for byte.
        """
        payload = loads(event.data)
        if payload is None:
            return event
        restored = walk_sync(payload, self._restore_full)
        if restored == payload:
            return event
        return replace(event, data=dumps(restored))


class LineRouter:
    """Rewrites an upstream JSON-lines stream (Ollama's), restoring it.

    The same contract as :class:`StreamRouter`: feed raw chunks in any
    splitting, get back whole lines to forward, then :meth:`flush` at the end.
    Each line is one object, restored by ``objects``, and re-serialized; a
    line that is not JSON is forwarded as it came, after any held-back text.
    """

    def __init__(self, objects: ObjectStream) -> None:
        self._objects = objects
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[str]:
        """Feed one raw upstream chunk; return the lines it completes."""
        text = self._utf8.decode(chunk) if isinstance(chunk, (bytes, bytearray)) else chunk
        *lines, self._buffer = (self._buffer + text).split("\n")
        return [out for line in lines for out in self._line(line)]

    def flush(self) -> list[str]:
        """The last line, if it came without a newline, and any held-back text."""
        rest = self._buffer + self._utf8.decode(b"", final=True)
        self._buffer = ""
        out = self._line(rest) if rest.strip() else []
        return out + [dumps(obj) + "\n" for obj in self._objects.flush()]

    def _line(self, line: str) -> list[str]:
        if not line.strip():
            return [line + "\n"]
        try:
            payload = json.loads(line)
        except ValueError:
            return [dumps(obj) + "\n" for obj in self._objects.flush()] + [line + "\n"]
        return [dumps(obj) + "\n" for obj in self._objects.feed(payload)]


def resolver_for(operator: Operator, mapping: dict[str, str]) -> Callable[[str], str | None]:
    """The token → original resolver of ``operator`` over ``mapping``.

    Most operators store the plaintext in the mapping, so the resolver is just
    ``mapping.get``.  An operator whose mapping value is not the plaintext
    (``encrypt`` stores ciphertext) overrides
    :meth:`~privyx.operators.Operator.build_resolver` to transform it.
    """
    return operator.build_resolver(mapping)


def select_processor_factory(
    operator: Operator,
    codec: FormatCodec,
    resolve: Callable[[str], str | None],
    mapping: dict[str, str],
) -> Callable[[], StreamDeanonymizer]:
    """Choose the streaming restorer for ``operator``.

    An operator whose :attr:`~privyx.operators.Operator.stream_restore` is
    ``"literal"`` (faker) substitutes ordinary text, so a stream is reversed by
    matching the exact values with a trie seeded from this session's ``mapping``.
    Everything else restores by codec syntax (pseudonym, hash) — the default.
    """
    if operator.stream_restore == "literal":
        return lambda: StreamingDeanonymizer(mapping)
    # A token's id may be written alone.  The session's tokens are indexed once
    # for all of this stream's processors, and again on its next stream: keep
    # the index with the session if a long one ever makes that show.
    ids = codec.ids(mapping)
    return lambda: TokenStreamProcessor(codec, resolve, ids)


def restore_chunks(chunks: list[Any], objects: ObjectStream) -> list[Any]:
    """Restore a stream sent as one JSON array of chunks (Gemini's without ``alt=sse``).

    Its chunks split text like SSE events do, so they are restored in order,
    as a stream.
    """
    restored: list[Any] = []
    for chunk in chunks:
        restored += objects.feed(chunk)
    return restored + objects.flush()


def reframe(event: SSEEvent, payload: dict[str, Any]) -> str:
    """``payload`` serialized into ``event``'s envelope (keeps ``event:``/``id:``)."""
    return replace(event, data=dumps(payload)).to_str()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def loads(data: str) -> dict[str, Any] | None:
    """``data`` parsed, when it is a JSON object."""
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True
