"""The ``anthropic`` dialect: the Anthropic Messages API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from privyx.core import JsonStrings, drive_sync
from privyx.dialects._base import CONTENT_KEYS, PREFIX_KEYS, Dialect
from privyx.streaming import SSEEvent, StreamDeanonymizer, StreamRouter, loads

if TYPE_CHECKING:
    from privyx.token import FormatCodec

#: Thinking blocks remembered at most, oldest dropped first.
_THINKING_MAX = 4096
#: Characters held in all, signatures included: bounded by count alone, 4096
#: long thinking blocks could hold hundreds of MB.
_THINKING_MAX_CHARS = 16_000_000

#: Characters of prefix worth a cache breakpoint: about 1024 tokens at four
#: characters a token, the smallest minimum most Claude models cache.
CACHE_MIN_CHARS = 4096
_EPHEMERAL = {"type": "ephemeral"}


def add_cache_control(payload: dict[str, Any]) -> None:
    """Mark a large ``tools`` / ``system`` prefix of an Anthropic request for caching, in place.

    Only a request that marks nothing itself is changed: then its last tool
    gets a breakpoint when ``tools`` is at least :data:`CACHE_MIN_CHARS` long,
    and its last ``system`` block when ``tools`` and ``system`` together are.
    Both are the same on every turn, so the request stays byte-stable.  A
    client that sets ``cache_control`` anywhere manages caching itself.
    """
    if "messages" not in payload or _has_cache_control(payload):
        return
    tools = payload.get("tools")
    size = 0
    if isinstance(tools, list) and tools:
        size = len(json.dumps(tools, ensure_ascii=False))
        if size >= CACHE_MIN_CHARS and isinstance(tools[-1], dict):
            tools[-1] = {**tools[-1], "cache_control": dict(_EPHEMERAL)}
    system = payload.get("system")
    if isinstance(system, str) and system:
        system = [{"type": "text", "text": system}]
    if not isinstance(system, list) or not system or not isinstance(system[-1], dict):
        return
    size += sum(len(str(block.get("text", ""))) for block in system if isinstance(block, dict))
    if size >= CACHE_MIN_CHARS:
        payload["system"] = [*system[:-1], {**system[-1], "cache_control": dict(_EPHEMERAL)}]


def _has_cache_control(value: Any) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(v) for v in value)
    return False


class AnthropicDialect(Dialect):
    """The Messages API: the key in ``x-api-key``, and thinking the upstream signed.

    The client gets a thinking block restored and echoes it back, but masking
    that again is not always the exact inverse (a value the model wrote itself
    is new to the detector), and the API rejects a thinking block whose text no
    longer matches its signature.  So the text the upstream signed is kept, by
    signature, and put back into an echoed block after the leak check.
    """

    name = "anthropic"
    assistant_json = True
    content_keys = CONTENT_KEYS
    prefix_keys = PREFIX_KEYS
    credential_header = "x-api-key"
    request_headers = {"anthropic-version": "2023-06-01"}

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        # Kept for this proxy's process: lost on restart, not shared between
        # workers; a signature error then means the vault should keep them.
        self._thinking: dict[str, str] = {}
        self._thinking_chars = 0

    def turn(self, path: str) -> bool:
        return not path.endswith("/count_tokens")

    def add_directive(self, payload: dict[str, Any], text: str, path: str) -> None:
        """After ``system`` (its first block may have to stay first)."""
        if "messages" not in payload:
            return
        system = payload.get("system")
        if isinstance(system, list):
            payload["system"] = [*system, {"type": "text", "text": text}]
        else:
            payload["system"] = f"{system}\n\n{text}" if system else text

    def add_cache_control(self, payload: dict[str, Any]) -> None:
        add_cache_control(payload)

    def reinstate(self, payload: dict[str, Any]) -> None:
        """Put the signed upstream text back into each echoed ``thinking`` block."""
        messages = payload.get("messages")
        for message in messages if isinstance(messages, list) else []:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    original = self._thinking.get(block.get("signature") or "")
                    if original is not None:
                        block["thinking"] = original

    def remember(self, payload: Any) -> None:
        content = payload.get("content") if isinstance(payload, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                self.remember_thinking(block.get("signature"), str(block.get("thinking", "")))

    def remember_thinking(self, signature: Any, text: str) -> None:
        """Keep the thinking ``text`` the upstream signed with ``signature``.

        The oldest go first once there are more than :data:`_THINKING_MAX` of
        them or they hold more than :data:`_THINKING_MAX_CHARS` characters.
        """
        if not isinstance(signature, str) or not signature:
            return
        if signature in self._thinking:
            self._thinking_chars -= len(signature) + len(self._thinking[signature])
        self._thinking[signature] = text
        self._thinking_chars += len(signature) + len(text)
        while len(self._thinking) > _THINKING_MAX or self._thinking_chars > _THINKING_MAX_CHARS:
            oldest = next(iter(self._thinking))
            self._thinking_chars -= len(oldest) + len(self._thinking.pop(oldest))

    def reply_text_is_json(self, request: Any) -> bool:
        config = request.get("output_config") if isinstance(request, dict) else None
        format_ = config.get("format") if isinstance(config, dict) else None
        return isinstance(format_, dict) and format_.get("type") == "json_schema"

    def stream(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer],
    ) -> StreamRouter:
        return AnthropicStream(codec, resolve, make_processor, remember=self.remember_thinking)


class AnthropicStream(StreamRouter):
    """A Messages stream: text and thinking deltas, and tool input buffered until whole.

    ``tool_use`` input streams as partial JSON; it is accumulated in pseudonym
    space and emitted, restored, at ``content_block_stop``.  Thinking text is
    restored on its own deanonymizer, so a held-back fragment never bleeds
    into visible text, and the text as sent is kept for its signature.
    ``message_stop`` goes last.
    """

    def __init__(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
        *,
        remember: Callable[[Any, str], None] = lambda signature, text: None,
    ) -> None:
        super().__init__(codec, resolve, make_processor)
        self._remember = remember
        self._think = self._make_processor()
        self._think_index: int | None = None
        # Content-block index -> accumulated partial_json.
        self._an_tools: dict[int, str] = {}
        self._json_blocks: dict[int, JsonStrings] = {}
        # Content-block index -> thinking text as sent (for its signature).
        self._an_thinking: dict[int, str] = {}
        self._block_types: dict[int, str] = {}

    def _event(self, event: SSEEvent) -> list[str]:
        payload = loads(event.data)
        if payload is None:
            return self._passthrough(event)
        return self._json(event, payload)

    def _wrap_text(self, text: str, template: SSEEvent) -> str:
        """``text`` as the ``text_delta`` of ``template``."""
        try:
            payload = json.loads(template.data)
        except json.JSONDecodeError:
            return template.to_str()
        if payload.get("type") != "content_block_delta":
            return template.to_str()
        delta_payload = payload.get("delta", {})
        if delta_payload.get("type") != "text_delta":
            return template.to_str()
        payload["delta"]["text"] = text
        return SSEEvent(
            data=json.dumps(payload, separators=(",", ":")),
            event=template.event,
            id=template.id,
            retry=template.retry,
        ).to_str()

    def _flush_held(self) -> list[str]:
        reasoning = self._flush_reasoning_tail()
        frames = [reasoning] if reasoning else []
        return frames + self._flush_json_blocks() + self._flush_anthropic_tools()

    def _flush_reasoning_tail(self) -> str | None:
        """An Anthropic thinking block the upstream never closed."""
        tail = self._think.flush()
        if not tail or self._think_index is None:
            return None
        return self._anthropic_delta_frame(self._think_index, "thinking_delta", "thinking", tail)

    def _json(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        ptype = payload.get("type")

        if ptype == "message_stop":
            self._hold_terminal(event)
            return []

        if ptype == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict):
                index = _index_of(payload)
                self._block_types[index] = str(block.get("type", ""))
                text = block.get("text")
                if (
                    self.json_text
                    and block.get("type") == "text"
                    and isinstance(text, str)
                    and text
                ):
                    block["text"] = ""
                    event.data = json.dumps(payload, separators=(",", ":"))
                    frames = self._passthrough(event, drain=False)
                    out = self._feed_json(index, text)
                    if out:
                        frames.append(self._anthropic_delta_frame(index, "text_delta", "text", out))
                    return frames
            return self._passthrough(event, drain=False)

        if ptype == "content_block_delta":
            return self._handle_anthropic_delta(event, payload)

        if ptype == "content_block_stop":
            index = _index_of(payload)
            return self._flush_anthropic_block(index) + [event.to_str()]

        if ptype in ("message_delta", "error") and self._json_blocks:
            return self._flush_json_blocks() + self._passthrough(event, drain=False)

        # message_start, ping, unknown.
        return self._passthrough(event, drain=False)

    def _handle_anthropic_delta(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return self._passthrough(event, drain=False)
        dtype = delta.get("type")

        if dtype == "text_delta":
            text = delta.get("text")
            if not isinstance(text, str) or not text:
                return self._passthrough(event, drain=False)
            if self.json_text:
                out = self._feed_json(_index_of(payload), text)
                return [self._wrap_text(out, event)] if out else []
            self._text_template = event
            out = self._text.feed(text)
            return [self._wrap_text(out, event)] if out else []

        if dtype == "thinking_delta":
            text = delta.get("thinking")
            if not isinstance(text, str) or not text:
                return self._passthrough(event, drain=False)
            index = _index_of(payload)
            self._think_index = index
            self._an_thinking[index] = self._an_thinking.get(index, "") + text
            out = self._think.feed(text)
            if not out:
                return []
            return [self._anthropic_delta_frame(index, "thinking_delta", "thinking", out)]

        if dtype == "input_json_delta":
            index = _index_of(payload)
            partial = delta.get("partial_json")
            if isinstance(partial, str) and partial:
                self._an_tools[index] = self._an_tools.get(index, "") + partial
            return []  # buffer; flushed at content_block_stop

        if dtype == "signature_delta":
            text = self._an_thinking.pop(_index_of(payload), "")
            self._remember(delta.get("signature"), text)

        # citations_delta (whole cited_text), signature_delta (recorded above), etc.
        return self._passthrough(event, drain=False)

    def _flush_anthropic_block(self, index: int) -> list[str]:
        block_type = self._block_types.get(index, "")

        if block_type == "tool_use" or index in self._an_tools:
            buffered = self._an_tools.pop(index, "")
            if not buffered:
                return []
            restored = self._restore_arguments(buffered)
            return [
                self._anthropic_delta_frame(index, "input_json_delta", "partial_json", restored)
            ]

        if block_type == "thinking":
            tail = self._think.flush()
            self._think_index = None
            if not tail:
                return []
            return [self._anthropic_delta_frame(index, "thinking_delta", "thinking", tail)]

        if index in self._json_blocks:
            return self._flush_json_block(index)

        # Default: a text block.
        tail = self._text.flush()
        if not tail or self._text_template is None:
            return []
        return [self._wrap_text(tail, self._text_template)]

    def _feed_json(self, index: int, text: str) -> str:
        strings = self._json_blocks.get(index)
        if strings is None:
            plain = self._make_processor()
            strings = self._json_blocks[index] = JsonStrings(
                restore_outside=True, plain_feed=plain.feed, plain_flush=plain.flush
            )
        restored: str = drive_sync(strings.feed(text), self._restore_full)
        return restored

    def _flush_json_block(self, index: int) -> list[str]:
        tail = drive_sync(self._json_blocks.pop(index).flush(), self._restore_full)
        return [self._anthropic_delta_frame(index, "text_delta", "text", tail)] if tail else []

    def _flush_json_blocks(self) -> list[str]:
        return [
            frame for index in list(self._json_blocks) for frame in self._flush_json_block(index)
        ]

    def _flush_anthropic_tools(self) -> list[str]:
        frames: list[str] = []
        for index in sorted(self._an_tools):
            buffered = self._an_tools[index]
            if buffered:
                restored = self._restore_arguments(buffered)
                frames.append(
                    self._anthropic_delta_frame(index, "input_json_delta", "partial_json", restored)
                )
        self._an_tools = {}
        return frames

    @staticmethod
    def _anthropic_delta_frame(index: int, dtype: str, key: str, text: str) -> str:
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": dtype, key: text},
        }
        return SSEEvent(
            data=json.dumps(payload, separators=(",", ":")),
            event="content_block_delta",
        ).to_str()


def _index_of(payload: dict[str, Any]) -> int:
    index = payload.get("index", 0)
    return index if isinstance(index, int) else 0
