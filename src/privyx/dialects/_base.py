"""The dialect kind: an API format, and where its conversation text is.

A dialect says which top-level keys of a request hold the conversation, which
keys inside them are structure rather than text, and how a reply in that
format is restored.  It never masks anything itself: the core walks the text
it points at, checks the request for leaks, and restores the reply.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from privyx.core import DOCUMENT_KEYS, Component

if TYPE_CHECKING:
    from privyx.streaming import ObjectStream, StreamDeanonymizer, StreamRouter
    from privyx.token import FormatCodec

#: The top-level keys walked first, in the order a provider caches the prompt.
PREFIX_KEYS: tuple[str, ...] = (
    "tools",
    "system",
    "systemInstruction",
    "system_instruction",
    "instructions",
)

#: Top-level request keys that carry conversation content, in every format but
#: Gemini: ``messages`` and ``system`` (Chat, Anthropic); ``input``,
#: ``instructions``, and ``prompt`` (template variables) for Responses;
#: ``prediction`` (Chat predicted output); ``prompt`` and ``suffix`` (legacy
#: completions); ``input`` (embeddings).  One set for all, so a key a format
#: does not document is still masked.
CONTENT_KEYS: frozenset[str] = frozenset(
    {"messages", "system", "input", "instructions", "prompt", "suffix", "prediction"}
)

#: Keys whose list of numbers is text sent as token ids, which cannot be scanned.
_TOKEN_ID_KEYS = ("input", "prompt")


class Dialect(Component, kind="dialect"):
    """An API format: where a request's text is, and how its reply is restored."""

    #: Top-level request keys holding conversation content.  An allowlist: a
    #: key missing here is forwarded as it is, so ``DialectSuite`` checks it.
    content_keys: ClassVar[frozenset[str]] = frozenset()
    #: Keys skipped with their subtree inside content, beside the ones every
    #: format has (ids, enums, signatures, binary): each one named.
    opaque_keys: ClassVar[frozenset[str]] = frozenset()
    #: Keys whose object value is a tool-argument document: no key is opaque inside.
    document_keys: ClassVar[frozenset[str]] = DOCUMENT_KEYS
    #: Top-level keys walked first, in this order, so the cached prompt prefix
    #: keeps its tokens when a later turn adds values.
    prefix_keys: ClassVar[tuple[str, ...]] = ()
    #: Top-level keys holding a whole request of this format, walked as one
    #: (Gemini's ``countTokens`` wraps the request it counts).
    wrapped_keys: ClassVar[frozenset[str]] = frozenset()
    #: Where the transparent proxy sends Privyx's key: ``bearer``, ``x-api-key``
    #: or ``x-goog-api-key``.
    credential_header: ClassVar[str] = "bearer"
    #: Paths a plugin's dialect adds to the default route map.
    default_routes: ClassVar[tuple[str, ...]] = ()
    #: Headers the transparent proxy adds to a request that has none of them.
    request_headers: ClassVar[Mapping[str, str]] = {}
    #: Whether a client may call the format's paths without their first
    #: segment, the API version, as one whose base URL ends in it does.
    versionless: ClassVar[bool] = False
    #: Whether the gateway serves the format: not one whose stream it cannot read.
    gateway: ClassVar[bool] = True
    #: Whether each request names its own endpoint (model and method in the
    #: path): the gateway serves it only with a provider that posts to the path called.
    own_endpoint: ClassVar[bool] = False
    #: Whether a batch reply holds text to restore (an embedding is numbers).
    restores_reply: ClassVar[bool] = True
    #: The media type of a stream of JSON lines, restored line by line, when
    #: the format streams so (Ollama); SSE is restored in every format.
    line_stream: ClassVar[str | None] = None

    def refuse(self, payload: Mapping[str, Any]) -> str | None:
        """Why a request cannot be masked, or ``None``.

        By default, one that sends token ids instead of text in ``input`` or
        ``prompt`` (``[1, 2]`` or ``[[1, 2]]``): only text is scanned, and the
        upstream decodes the ids back to the original.
        """
        for key in _TOKEN_ID_KEYS:
            value = payload.get(key)
            for item in value if isinstance(value, list) else []:
                if isinstance(item, int) or (
                    isinstance(item, list) and any(isinstance(i, int) for i in item)
                ):
                    return (
                        "the request sends token ids instead of text, so privyx could not "
                        "mask it and did not forward it; send the text instead"
                    )
        return None

    def wants_stream(self, path: str, payload: Any, query: Any) -> bool:
        """Whether a request asks for its reply as SSE: by default, ``"stream": true``."""
        return isinstance(payload, dict) and bool(payload.get("stream", False))

    def turn(self, path: str) -> bool:
        """Whether a request to ``path`` is one model turn, which a budget counts."""
        return True

    def ask_usage(self, payload: dict[str, Any]) -> None:
        """Ask a request, in place, to report its token usage, when the format must be asked."""

    def conversation_key(self, payload: Mapping[str, Any]) -> str:
        """Text that stays the same on every turn of a conversation: its first user message.

        ``""`` when there is none.  It keys the ``conversation`` session strategy.
        """
        messages = payload.get("messages", payload.get("input"))
        if isinstance(messages, str):
            return messages
        if not isinstance(messages, list):
            return ""
        for message in messages:
            if isinstance(message, Mapping) and message.get("role") == "user":
                return _content_text(message.get("content"))
        return ""

    def error_body(self, status: int, code: str, message: str) -> Any:
        """An error Privyx answers a client with, in the client's format."""
        return {"error": {"type": code, "message": message}}

    def add_directive(self, payload: dict[str, Any], text: str, path: str) -> None:
        """Add ``text`` to the request's system prompt, in place, where it stays put."""

    def add_cache_control(self, payload: dict[str, Any]) -> None:
        """Mark the request's cacheable prefix, in place, when the format marks one."""

    def reinstate(self, payload: dict[str, Any]) -> None:
        """Put back, in the masked request, text the upstream signed: after the leak check."""

    def remember(self, payload: Any) -> None:
        """Note what a batch reply holds that a later request echoes, before it is restored."""

    def objects(
        self, make_processor: Callable[[], StreamDeanonymizer], restore: Callable[[str], str]
    ) -> ObjectStream | None:
        """What restores a stream of whole JSON objects in this format (JSON lines,
        an array of chunks), or ``None`` when it sends none."""
        return None

    def stream(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer],
    ) -> StreamRouter:
        """What restores a streamed reply in this format, event by event."""
        from privyx.streaming import StreamRouter

        return StreamRouter(codec, resolve, make_processor)


def _content_text(content: Any) -> str:
    """A message ``content``, a string or a list of text parts, as text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, Mapping)
            and part.get("type") in ("text", "input_text")
            and isinstance(part.get("text"), str)
        )
    return ""
