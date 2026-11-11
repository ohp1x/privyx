"""Restore a stream of whole JSON objects whose text arrives in pieces.

Gemini streams a reply as ``GenerateContentResponse`` chunks, as SSE events or
as the elements of one JSON array, and Ollama as one JSON object per line.
Each object is complete, but the reply's text is cut across objects anywhere,
so a pseudonym can start in one and end in the next.  :class:`GeminiStream`
and :class:`OllamaStream` restore such a stream one object at a time: each
text field goes through its own deanonymizer, which holds back a fragment
that may still grow into a token, and every other string (a function or tool
call comes whole) is restored in place.

A held-back fragment is given back when other content follows, when the reply
finishes, and, if the upstream ends the stream without finishing, in one last
object of its own.  The framing (SSE event, array element, line) stays with
the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from privyx.proxy.schemas import walk_sync
from privyx.streaming.deanonymizer import StreamDeanonymizer

#: Top-level fields of a chunk copied into the chunk that carries a fragment
#: held back to the end of the stream.
_TEMPLATE_KEYS = ("modelVersion", "responseId")
#: Top-level fields a chunk whose text is all held back may be dropped with:
#: they come again, up to date, in the chunks that follow.
_REPEATED_KEYS = frozenset(
    {"candidates", "modelVersion", "responseId", "usageMetadata", "createTime"}
)


class GeminiStream:
    """Restores the Gemini chunks of one streamed reply.

    Args:
        make_processor: Builds the deanonymizer for one candidate's text, or
            for its thought summary.
        restore: Restores a complete string.
    """

    def __init__(
        self,
        make_processor: Callable[[], StreamDeanonymizer],
        restore: Callable[[str], str],
    ) -> None:
        self._make_processor = make_processor
        self._restore = restore
        # (candidate index, "text" or "thought") -> its deanonymizer.
        self._held: dict[tuple[Any, str], StreamDeanonymizer] = {}
        self._template: dict[str, Any] = {}

    def feed(self, payload: Any) -> list[Any]:
        """Restore one chunk; return the chunks to send in its place (zero to two)."""
        if not isinstance(payload, dict):
            return [self._whole(payload)]
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            # An error or a blocked prompt: what was held back comes first.
            return self.flush() + [self._whole(payload)]
        self._template = {k: payload[k] for k in _TEMPLATE_KEYS if k in payload}
        texts = 0
        emitted = False
        rebuilt: list[Any] = []
        for position, candidate in enumerate(candidates):
            content = candidate.get("content") if isinstance(candidate, dict) else None
            if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
                rebuilt.append(self._whole(candidate))
                emitted = True
                continue
            index = candidate.get("index", position)
            parts: list[Any] = []
            for part in content["parts"]:
                kind = _text_kind(part)
                if kind is None:
                    parts += self._drain(index)
                    parts.append(self._whole(part))
                    continue
                texts += 1
                parts += self._drain(index, keep=kind)
                text = self._processor(index, kind).feed(part["text"])
                if text or set(part) - {"text", "thought"}:  # a thoughtSignature must go on
                    rest = self._whole({k: v for k, v in part.items() if k != "text"})
                    parts.append({k: text if k == "text" else rest[k] for k in part})
            if candidate.get("finishReason"):
                parts += self._drain(index)
            new_content = self._whole({k: v for k, v in content.items() if k != "parts"})
            new_content["parts"] = parts
            new_candidate = self._whole({k: v for k, v in candidate.items() if k != "content"})
            new_candidate["content"] = new_content
            rebuilt.append({k: new_candidate[k] for k in candidate})
            # Anything but its text (citations, safety ratings) goes out too.
            emitted = emitted or bool(parts) or bool(set(candidate) - {"content", "index"})
        if texts and not emitted and set(payload) <= _REPEATED_KEYS:
            return []  # all of its text is held back, and nothing else is new
        out = self._whole({k: v for k, v in payload.items() if k != "candidates"})
        out["candidates"] = rebuilt
        return [{k: out[k] for k in payload}]

    def flush(self) -> list[Any]:
        """The text still held back, as one chunk, or nothing."""
        candidates: dict[Any, list[dict[str, Any]]] = {}
        for index, _ in list(self._held):
            parts = self._drain(index)
            if parts:
                candidates.setdefault(index, []).extend(parts)
        if not candidates:
            return []
        chunk = {
            "candidates": [
                {"content": {"role": "model", "parts": parts}, "index": index}
                for index, parts in candidates.items()
            ],
            **self._whole(self._template),
        }
        return [chunk]

    def _processor(self, index: Any, kind: str) -> StreamDeanonymizer:
        processor = self._held.get((index, kind))
        if processor is None:
            processor = self._held[(index, kind)] = self._make_processor()
        return processor

    def _drain(self, index: Any, keep: str | None = None) -> list[dict[str, Any]]:
        """Parts with what candidate ``index`` holds back, but for ``keep``."""
        parts: list[dict[str, Any]] = []
        for kind in ("thought", "text"):
            processor = self._held.get((index, kind))
            if kind == keep or processor is None:
                continue
            tail = processor.flush()
            if tail:
                parts.append({"text": tail, **({"thought": True} if kind == "thought" else {})})
        return parts

    def _whole(self, value: Any) -> Any:
        return walk_sync(value, self._restore, schema="gemini")


def _text_kind(part: Any) -> str | None:
    """``"text"`` or ``"thought"`` for a part with text, else ``None``."""
    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
        return None
    return "thought" if part.get("thought") is True else "text"


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
        return walk_sync(value, self._restore, schema="ollama")
