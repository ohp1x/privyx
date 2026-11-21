"""The ``gemini`` dialect: the Gemini API, and its stream of whole response chunks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from privyx.core import walk_sync
from privyx.dialects._base import PREFIX_KEYS, Dialect
from privyx.streaming import SSEEvent, StreamDeanonymizer, StreamRouter, dumps, loads, reframe

if TYPE_CHECKING:
    from privyx.streaming import ObjectStream
    from privyx.token import FormatCodec

#: Top-level keys of a request that carry the conversation, in both of the
#: casings Gemini accepts.  ``generateContentRequest`` is the request a
#: ``countTokens`` call wraps; it is walked as a request of its own.
#: ``content`` and ``title`` are an embedding request's text, and ``requests``
#: the embedding requests of a batch, walked whole.
_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "contents",
        "systemInstruction",
        "system_instruction",
        "generateContentRequest",
        "generate_content_request",
        "requests",
        "content",
        "title",
    }
)
_WRAPPED = frozenset({"generateContentRequest", "generate_content_request"})
#: ``functionCall.args`` and ``functionResponse.response``: tool-argument documents.
_DOCUMENT_KEYS = frozenset({"args", "response"})
#: The media type and URI of a file part, and the signature of the model's
#: hidden reasoning.
_OPAQUE_KEYS = frozenset(
    {
        "mimeType",
        "mime_type",
        "fileUri",
        "file_uri",
        "thoughtSignature",
        "thought_signature",
    }
)
#: Path endings of requests that are no model turn: token counting, embeddings,
#: and cached contents, which share the chat format.
_FREE = (":countTokens", ":embedContent", ":batchEmbedContents", "/cachedContents")


class GeminiDialect(Dialect):
    """The Gemini API: ``contents`` and ``parts``, the key in ``x-goog-api-key``."""

    name = "gemini"
    content_keys = _CONTENT_KEYS
    opaque_keys = _OPAQUE_KEYS
    document_keys = _DOCUMENT_KEYS
    prefix_keys = PREFIX_KEYS
    wrapped_keys = _WRAPPED
    credential_header = "x-goog-api-key"
    versionless = True
    own_endpoint = True

    def wants_stream(self, path: str, payload: Any, query: Any) -> bool:
        """It streams on its own method, as SSE only with ``alt=sse`` (what its SDKs send).

        Without it the reply is a JSON array, relayed as one body.
        """
        return path.endswith(":streamGenerateContent") and dict(query).get("alt") == "sse"

    def turn(self, path: str) -> bool:
        return not path.endswith(_FREE)

    def conversation_key(self, payload: Any) -> str:
        """Text of the first ``user`` turn of ``contents`` (``role`` may be left out)."""
        return _gemini_first_user_text(payload.get("contents"))

    def add_directive(self, payload: dict[str, Any], text: str, path: str) -> None:
        """Last in ``systemInstruction``; of the request a ``countTokens`` call wraps."""
        if path.endswith(":countTokens"):
            for key in _WRAPPED:
                request = payload.get(key)
                if isinstance(request, dict):
                    _add_gemini_directive(request, text)
        else:
            _add_gemini_directive(payload, text)

    def objects(
        self, make_processor: Callable[[], StreamDeanonymizer], restore: Callable[[str], str]
    ) -> ObjectStream:
        return GeminiStream(make_processor, restore)

    def stream(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer],
    ) -> StreamRouter:
        return GeminiSSE(codec, resolve, make_processor)


class GeminiSSE(StreamRouter):
    """A Gemini stream as SSE (``alt=sse``): each event one whole response chunk."""

    def __init__(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
    ) -> None:
        super().__init__(codec, resolve, make_processor)
        self._objects = GeminiStream(self._make_processor, self._restore_full)

    def _event(self, event: SSEEvent) -> list[str]:
        payload = loads(event.data)
        if payload is None:
            return self._passthrough(event)
        return [reframe(event, chunk) for chunk in self._objects.feed(payload)]

    def _flush_held(self) -> list[str]:
        return [SSEEvent(data=dumps(chunk)).to_str() for chunk in self._objects.flush()]


def _add_gemini_directive(request: dict[str, Any], text: str) -> None:
    """Add ``text`` to a Gemini request's ``systemInstruction``, in place.

    Only to a request with ``contents``, and not to one that uses a cached
    content: Gemini refuses a ``systemInstruction`` beside it (the directive
    was added to the cache when it was created through Privyx).
    """
    if "contents" not in request or "cachedContent" in request or "cached_content" in request:
        return
    key = "system_instruction" if "system_instruction" in request else "systemInstruction"
    system = request.get(key)
    if system is None:
        request[key] = {"parts": [{"text": text}]}
    elif isinstance(system, dict) and isinstance(system.get("parts"), list):
        request[key] = {**system, "parts": [*system["parts"], {"text": text}]}


def _gemini_first_user_text(contents: Any) -> str:
    """Text of the first ``user`` turn of Gemini ``contents`` (``role`` may be left out)."""
    for content in contents if isinstance(contents, list) else []:
        if isinstance(content, Mapping) and content.get("role", "user") == "user":
            parts = content.get("parts")
            return "".join(
                part["text"]
                for part in (parts if isinstance(parts, list) else [])
                if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            )
    return ""


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
        return walk_sync(value, self._restore, documents=_DOCUMENT_KEYS, hidden=_OPAQUE_KEYS)


def _text_kind(part: Any) -> str | None:
    """``"text"`` or ``"thought"`` for a part with text, else ``None``."""
    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
        return None
    return "thought" if part.get("thought") is True else "text"
