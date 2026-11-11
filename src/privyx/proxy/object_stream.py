"""Restore a stream of whole JSON objects whose text arrives in pieces.

Gemini streams a reply as ``GenerateContentResponse`` chunks, as SSE events or
as the elements of one JSON array.  Each chunk is complete, but the reply's
text is cut across chunks anywhere, so a pseudonym can start in one and end in
the next.  :class:`GeminiStream` restores such a stream one chunk at a time:
each text field goes through its own deanonymizer, which holds back a fragment
that may still grow into a token, and every other string (a function call
comes whole) is restored in place.

A held-back fragment is given back when other content follows, when the reply
finishes, and, if the upstream ends the stream without finishing, in one last
chunk of its own.  The framing (SSE event, array element) stays with the
caller.
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

