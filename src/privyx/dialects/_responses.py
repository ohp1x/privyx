"""The ``responses`` dialect: the OpenAI Responses API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from privyx.dialects._base import CONTENT_KEYS, PREFIX_KEYS, Dialect
from privyx.streaming import SSEEvent, StreamDeanonymizer, StreamRouter, loads, reframe

if TYPE_CHECKING:
    from privyx.token import FormatCodec

#: Delta kinds carrying tool-call input: buffered until ``.done``.
_RS_BUFFERED = frozenset(
    {
        "response.function_call_arguments",
        "response.mcp_call_arguments",
        "response.custom_tool_call_input",
    }
)
#: Events that end the stream: every held-back delta is flushed first.
_RS_TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed", "error"})
#: Fields that, with the event kind, identify one delta stream.
_RS_INDEXES = ("item_id", "output_index", "content_index", "summary_index", "command_index")


class ResponsesDialect(Dialect):
    """The Responses API: ``input`` items, ``instructions``, and typed ``response.*`` events."""

    name = "responses"
    content_keys = CONTENT_KEYS
    prefix_keys = PREFIX_KEYS

    def turn(self, path: str) -> bool:
        return not path.endswith("/input_tokens")

    def add_directive(self, payload: dict[str, Any], text: str, path: str) -> None:
        """First in ``input``; in ``instructions`` for a request that resumes a stored response.

        Such a request (``previous_response_id``) sends only its new items, and
        ``instructions`` are not carried over.
        """
        if "input" not in payload:
            return
        instructions = payload.get("instructions")
        if payload.get("previous_response_id"):
            payload["instructions"] = f"{instructions}\n\n{text}" if instructions else text
            return
        items = payload["input"]
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        message = {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        }
        payload["input"] = [message, *items] if isinstance(items, list) else items

    def stream(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer],
    ) -> StreamRouter:
        return ResponsesStream(codec, resolve, make_processor)


@dataclass(slots=True)
class _Delta:
    """One delta stream: its latest event (the re-wrap template), and a text
    deanonymizer — or ``None`` when tool input is buffered instead."""

    event: SSEEvent
    payload: dict[str, Any]
    processor: StreamDeanonymizer | None
    buffer: str = ""


class ResponsesStream(StreamRouter):
    """A Responses stream: each delta stream restored on its own, tool input buffered."""

    def __init__(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
    ) -> None:
        super().__init__(codec, resolve, make_processor)
        # (kind, *indexes) -> its delta stream.
        self._rs: dict[tuple[Any, ...], _Delta] = {}

    def _event(self, event: SSEEvent) -> list[str]:
        payload = loads(event.data)
        if payload is None:
            return self._passthrough(event)
        return self._json(event, payload)

    def _flush_held(self) -> list[str]:
        return self._flush_responses()

    def _json(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        """Typed ``response.*`` events: deltas per stream, the rest restored whole.

        A delta stream is keyed by its kind (``type`` minus ``.delta``) and the
        indexes present, so parallel parts never share a held-back tail.  Its
        ``.done`` event (same kind and indexes) flushes that tail first.
        """
        ptype = str(payload.get("type", ""))
        if ptype == "response.audio.delta":
            return [event.to_str()]  # base64 audio: no text to restore
        kind, _, suffix = ptype.rpartition(".")
        key = (kind, *(payload.get(name) for name in _RS_INDEXES))
        delta = payload.get("delta")

        if isinstance(delta, str):
            slot = self._rs.get(key)
            if slot is None:
                processor = None if kind in _RS_BUFFERED else self._make_processor()
                slot = self._rs[key] = _Delta(event, payload, processor)
            slot.event, slot.payload = event, payload
            if slot.processor is None:
                slot.buffer += delta
                return []  # buffer; flushed at .done
            out = slot.processor.feed(delta)
            return [self._responses_delta_frame(slot, out)] if out else []

        if suffix == "done":
            return self._flush_responses(key) + [self._restore_event(event).to_str()]
        if ptype in _RS_TERMINAL:
            return self._flush_responses() + [self._restore_event(event).to_str()]
        # Object deltas (shell_call_output_content's stdout/stderr) are restored
        # per event, so a token split across two of them reaches the client as
        # fragments (its .done is whole); give them processors if that is seen.
        return [self._restore_event(event).to_str()]

    def _flush_responses(self, key: tuple[Any, ...] | None = None) -> list[str]:
        """Emit the held-back tail of one delta stream (``key``), or of all."""
        keys = [key] if key is not None else list(self._rs)
        frames: list[str] = []
        for k in keys:
            slot = self._rs.pop(k, None)
            if slot is None:
                continue
            if slot.processor is None:
                text = self._restore_arguments(slot.buffer)
            else:
                text = slot.processor.flush()
            if text:
                frames.append(self._responses_delta_frame(slot, text))
        return frames

    @staticmethod
    def _responses_delta_frame(slot: _Delta, text: str) -> str:
        # An extra flushed frame repeats its template's sequence_number; clients
        # key deltas by item/index, not by a gap-free sequence, so it is harmless.
        return reframe(slot.event, {**slot.payload, "delta": text})
