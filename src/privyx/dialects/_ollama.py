"""The ``ollama`` dialect: Ollama's own API, and its stream of JSON lines."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from privyx.core import walk_sync
from privyx.dialects._base import CONTENT_KEYS, PREFIX_KEYS, Dialect

if TYPE_CHECKING:
    from privyx.streaming import ObjectStream, StreamDeanonymizer


class OllamaDialect(Dialect):
    """``/api/chat`` and ``/api/generate``: base64 images, and replies as JSON lines.

    It takes no directive: a system prompt there replaces the one the model
    was made with.
    """

    name = "ollama"
    content_keys = CONTENT_KEYS
    #: Base64 images, and the tool a tool message answers.
    opaque_keys = frozenset({"images", "tool_name"})
    prefix_keys = PREFIX_KEYS
    line_stream = "application/x-ndjson"
    #: Its streams are JSON lines, and a server of its own has no base path or key.
    gateway = False

    def objects(
        self, make_processor: Callable[[], StreamDeanonymizer], restore: Callable[[str], str]
    ) -> ObjectStream:
        return OllamaStream(make_processor, restore)


#: Ollama's streamed text fields, in the order they come: ``/api/chat`` sends
#: them in ``message``, ``/api/generate`` at the top level.
_CHAT_FIELDS = ("thinking", "content")
_GENERATE_FIELDS = ("thinking", "response")


class OllamaStream:
    """Restores the lines of one streamed Ollama reply.

    Args:
        make_processor: Builds the deanonymizer for one text field.
        restore: Restores a complete string.
    """

    def __init__(
        self,
        make_processor: Callable[[], StreamDeanonymizer],
        restore: Callable[[str], str],
    ) -> None:
        self._make_processor = make_processor
        self._restore = restore
        self._held: dict[str, StreamDeanonymizer] = {}
        self._chat = True  # the shape of the last line, for a tail sent alone
        self._model: Any = None

    def feed(self, payload: Any) -> list[Any]:
        """Restore one line's object; return the objects to send in its place."""
        if not isinstance(payload, dict):
            return [self._whole(payload)]
        if "error" in payload:
            return self.flush() + [self._whole(payload)]
        message = payload.get("message")
        self._chat = isinstance(message, dict)
        self._model = payload.get("model", self._model)
        holder: dict[str, Any] = message if isinstance(message, dict) else payload
        names = _CHAT_FIELDS if self._chat else _GENERATE_FIELDS
        texts = {n: holder[n] for n in names if isinstance(holder.get(n), str) and holder[n]}
        out = dict.fromkeys(names, "")
        for name in names:
            if name in texts:
                # One field starting ends the others: what they hold goes first.
                for other in names:
                    if other != name:
                        out[other] += self._flush(other)
                out[name] += self._processor(name).feed(texts[name])
        if payload.get("done") is True or holder.get("tool_calls"):
            for name in names:
                out[name] += self._flush(name)
        rebuilt = self._fill(holder, names, out)
        if holder is payload:
            return [rebuilt]
        rest = self._whole({k: v for k, v in payload.items() if k != "message"})
        return [{k: rebuilt if k == "message" else rest[k] for k in payload}]

    def flush(self) -> list[Any]:
        """The text still held back, as one more object, or nothing."""
        names = _CHAT_FIELDS if self._chat else _GENERATE_FIELDS
        tails = {name: self._flush(name) for name in names}
        tails = {name: tail for name, tail in tails.items() if tail}
        if not tails:
            return []
        body = {"message": {"role": "assistant", **tails}} if self._chat else tails
        head = {"model": self._model} if self._model is not None else {}
        return [{**self._whole(head), **body, "done": False}]

    def _fill(self, holder: dict[str, Any], names: tuple[str, ...], out: dict[str, str]) -> Any:
        """``holder`` restored, with ``out`` in its text fields."""
        rest = self._whole({k: v for k, v in holder.items() if k not in names})
        rebuilt = {
            k: out[k] if k in names and (isinstance(v, str) or out[k]) else rest.get(k, v)
            for k, v in holder.items()
        }
        for name in names:
            if out[name] and name not in rebuilt:
                rebuilt[name] = out[name]
        return rebuilt

    def _processor(self, name: str) -> StreamDeanonymizer:
        processor = self._held.get(name)
        if processor is None:
            processor = self._held[name] = self._make_processor()
        return processor

    def _flush(self, name: str) -> str:
        processor = self._held.get(name)
        return processor.flush() if processor is not None else ""

    def _whole(self, value: Any) -> Any:
        return walk_sync(value, self._restore, hidden=OllamaDialect.opaque_keys)
