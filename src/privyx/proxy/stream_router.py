"""Streaming SSE transformer shared by the gateway and the transparent proxy.

:class:`StreamRouter` turns a stream of raw upstream chunks into a stream of
deanonymized SSE frames.  It is the one place that reconciles three buffers whose
boundaries never line up — bytes (a UTF-8 char may split across chunks), SSE
events (an event may split across chunks), and tokens (a pseudonym may split
across events) — and adds two more concerns on top:

* **Held-back text ordering.** The deanonymizer holds a trailing fragment that
  might still grow into a token; that fragment is flushed *before* any pass-through
  event and at end of stream, never after, so nothing arrives out of order.
* **Tool-call buffering.** OpenAI ``tool_calls`` and Anthropic ``tool_use`` stream
  their arguments as partial JSON across many events.  Forwarding the partials
  would leak pseudonyms and fragment the JSON, so they are accumulated in
  pseudonym space and emitted — deanonymized, in one frame — only once complete.

Terminal events (OpenAI ``[DONE]``, Anthropic ``message_stop``) are deferred and
emitted last, after every held-back fragment and buffered tool call has been
flushed.  (A previous approach that tried to defer ``[DONE]`` *after* JSON-parsing
it never fired, because ``[DONE]`` is not JSON — here it is matched as a raw
sentinel before any parsing.)

The class is deliberately synchronous: token resolution uses the session's
pseudonym map captured once at stream start (the response only ever references
tokens minted while transforming the matching request), so no ``await`` is
needed per event.
"""

from __future__ import annotations

import codecs
import copy
import json
from collections.abc import Callable
from typing import Any

from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.deanonymizer import (
    StreamDeanonymizer,
    StreamingDeanonymizer,
    TokenStreamProcessor,
)
from privyx.streaming.sse import SSEDecoder, SSEEvent
from privyx.token.codec import TokenCodec

_DONE = "[DONE]"


class StreamRouter:
    """Rewrites an upstream SSE stream, deanonymizing text and tool calls.

    Args:
        codec: Token codec (syntax authority) shared with the engine.
        resolve: Maps a token's text to its original value, or ``None`` when the
            token is unknown to this session (then it is passed through verbatim).
        adapter: Stream adapter for the provider; its ``schema_name`` selects the
            event-classification strategy and it locates the primary text delta.
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
        codec: TokenCodec,
        resolve: Callable[[str], str | None],
        adapter: SSEStreamAdapter,
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
    ) -> None:
        self._codec = codec
        self._resolve = resolve
        self._adapter = adapter
        self._schema = getattr(adapter, "schema_name", "generic")
        self._make_processor = make_processor or (lambda: TokenStreamProcessor(codec, resolve))

        self._decoder = SSEDecoder()
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")

        # Two independent deanonymizers so a held-back fragment in "thinking"
        # text never bleeds into visible text (and vice versa).
        self._text = self._make_processor()
        self._think = self._make_processor()

        # Templates for re-wrapping a flushed tail into the provider's schema.
        self._text_template: SSEEvent | None = None
        self._reasoning_payload: dict[str, Any] | None = None  # OpenAI
        self._think_index: int | None = None  # Anthropic

        self._pending_terminal: list[str] = []
        # OpenAI: tool-call index -> {"id", "name", "args"} (pseudonym space).
        self._oa_tools: dict[int, dict[str, str]] = {}
        # Anthropic: content-block index -> accumulated partial_json.
        self._an_tools: dict[int, str] = {}
        self._block_types: dict[int, str] = {}

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

        tail = self._flush_text_tail()
        if tail:
            out.append(tail)
        reasoning = self._flush_reasoning_tail()
        if reasoning:
            out.append(reasoning)
        out.extend(self._flush_openai_tools())
        out.extend(self._flush_anthropic_tools())
        out.extend(self._pending_terminal)
        self._pending_terminal = []
        return out

    # -- dispatch ----------------------------------------------------------

    def _handle(self, event: SSEEvent) -> list[str]:
        if event.data.strip() == _DONE:
            self._pending_terminal.append(event.to_str())
            return []
        if self._schema == "generic":
            return self._text_via_adapter(event)
        payload = _loads(event.data)
        if payload is None:
            return self._passthrough(event)
        if self._schema == "anthropic":
            return self._handle_anthropic(event, payload)
        return self._handle_openai(event, payload)

    # -- primary text (generic + OpenAI content + Anthropic text_delta) -----

    def _text_via_adapter(self, event: SSEEvent) -> list[str]:
        delta = self._adapter.extract_delta(event)
        if not delta:
            return self._passthrough(event)
        self._text_template = event
        out = self._text.feed(delta)
        return [self._adapter.wrap_delta(out, event).to_str()] if out else []

    def _flush_text_tail(self) -> str | None:
        tail = self._text.flush()
        if not tail or self._text_template is None:
            return None
        return self._adapter.wrap_delta(tail, self._text_template).to_str()

    def _passthrough(self, event: SSEEvent, *, drain: bool = True) -> list[str]:
        """Forward a non-transformed event.

        ``drain`` flushes any held-back text first so ordering is preserved; it
        is disabled for Anthropic control events (e.g. ``ping``) that may arrive
        *between* the chunks of a single token, where flushing would split it.
        """
        out: list[str] = []
        if drain:
            tail = self._flush_text_tail()
            if tail:
                out.append(tail)
        out.append(event.to_str())
        return out

    # -- OpenAI ------------------------------------------------------------

    def _handle_openai(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return self._passthrough(event)
        choice = choices[0]
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            self._accumulate_openai_tools(tool_calls)
            if choice.get("finish_reason"):
                return self._flush_openai_tools() + [self._openai_finish_frame(payload)]
            return []  # still accumulating — suppress the partial

        if choice.get("finish_reason") and self._oa_tools:
            # Tools closed by a separate finish chunk carrying no tool_calls.
            return self._flush_openai_tools() + self._passthrough(event)

        content = delta.get("content")
        if isinstance(content, str) and content:
            self._text_template = event
            out = self._text.feed(content)
            return [self._adapter.wrap_delta(out, event).to_str()] if out else []

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._reasoning_payload = payload
            out = self._think.feed(reasoning)
            return [self._wrap_openai_field(payload, "reasoning_content", out)] if out else []

        return self._passthrough(event)  # role-only / usage / finish

    def _accumulate_openai_tools(self, tool_calls: list[Any]) -> None:
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            if not isinstance(index, int):
                index = 0
            slot = self._oa_tools.setdefault(index, {"id": "", "name": "", "args": ""})
            if isinstance(call.get("id"), str):
                slot["id"] = call["id"]
            function = call.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    slot["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    slot["args"] += function["arguments"]

    def _flush_openai_tools(self) -> list[str]:
        """Emit complete, valid-JSON tool calls; drop truncated ones.

        Forwarding a truncated tool call makes clients refuse it ("truncated
        tool call detected"), so args that do not parse are dropped — the client
        sees ``finish_reason`` and retries.
        """
        frames: list[str] = []
        for index in sorted(self._oa_tools):
            args = self._oa_tools[index]["args"]
            if not args:
                continue
            restored = self._restore_full(args)
            if not _is_valid_json(restored):
                continue
            slot = self._oa_tools[index]
            frames.append(self._openai_tool_frame(index, slot["id"], slot["name"], restored))
        self._oa_tools = {}
        return frames

    def _flush_reasoning_tail(self) -> str | None:
        tail = self._think.flush()
        if not tail:
            return None
        if self._reasoning_payload is not None:  # OpenAI
            return self._wrap_openai_field(self._reasoning_payload, "reasoning_content", tail)
        if self._think_index is not None:  # Anthropic thinking block never closed
            return self._anthropic_delta_frame(
                self._think_index, "thinking_delta", "thinking", tail
            )
        return None

    @staticmethod
    def _openai_finish_frame(payload: dict[str, Any]) -> str:
        """The finish chunk with its ``delta`` emptied of buffered tool_calls."""
        new = copy.deepcopy(payload)
        choices = new.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choices[0]["delta"] = {}
        return SSEEvent(data=json.dumps(new, separators=(",", ":"))).to_str()

    @staticmethod
    def _openai_tool_frame(index: int, call_id: str, name: str, arguments: str) -> str:
        payload = {
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ]
                    },
                }
            ],
        }
        return SSEEvent(data=json.dumps(payload, separators=(",", ":"))).to_str()

    @staticmethod
    def _wrap_openai_field(payload: dict[str, Any], field: str, text: str) -> str:
        new = copy.deepcopy(payload)
        choices = new.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                delta = {}
                choices[0]["delta"] = delta
            delta[field] = text
        return SSEEvent(data=json.dumps(new, separators=(",", ":"))).to_str()

    # -- Anthropic ---------------------------------------------------------

    def _handle_anthropic(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        ptype = payload.get("type")

        if ptype == "message_stop":
            self._pending_terminal.append(event.to_str())
            return []

        if ptype == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict):
                self._block_types[_index_of(payload)] = str(block.get("type", ""))
            return self._passthrough(event, drain=False)

        if ptype == "content_block_delta":
            return self._handle_anthropic_delta(event, payload)

        if ptype == "content_block_stop":
            index = _index_of(payload)
            return self._flush_anthropic_block(index) + [event.to_str()]

        # message_start, message_delta, ping, error, unknown.
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
            self._text_template = event
            out = self._text.feed(text)
            return [self._adapter.wrap_delta(out, event).to_str()] if out else []

        if dtype == "thinking_delta":
            text = delta.get("thinking")
            if not isinstance(text, str) or not text:
                return self._passthrough(event, drain=False)
            index = _index_of(payload)
            self._think_index = index
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

        return self._passthrough(event, drain=False)  # signature_delta etc.

    def _flush_anthropic_block(self, index: int) -> list[str]:
        block_type = self._block_types.get(index, "")

        if block_type == "tool_use" or index in self._an_tools:
            buffered = self._an_tools.pop(index, "")
            if not buffered:
                return []
            restored = self._restore_full(buffered)
            return [
                self._anthropic_delta_frame(index, "input_json_delta", "partial_json", restored)
            ]

        if block_type == "thinking":
            tail = self._think.flush()
            self._think_index = None
            if not tail:
                return []
            return [self._anthropic_delta_frame(index, "thinking_delta", "thinking", tail)]

        # Default: a text block.
        tail = self._text.flush()
        if not tail or self._text_template is None:
            return []
        return [self._adapter.wrap_delta(tail, self._text_template).to_str()]

    def _flush_anthropic_tools(self) -> list[str]:
        frames: list[str] = []
        for index in sorted(self._an_tools):
            buffered = self._an_tools[index]
            if buffered:
                restored = self._restore_full(buffered)
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

    # -- shared ------------------------------------------------------------

    def _restore_full(self, text: str) -> str:
        """Deanonymize a complete string with the same recognition as text."""
        processor = self._make_processor()
        return processor.feed(text) + processor.flush()


def select_processor_factory(
    operator: object,
    codec: TokenCodec,
    resolve: Callable[[str], str | None],
    mapping: dict[str, str],
) -> Callable[[], StreamDeanonymizer]:
    """Choose the streaming restorer for ``operator``.

    An operator whose :attr:`~privyx.privacy.operator.base.BaseOperator.stream_restore`
    is ``"literal"`` (faker) substitutes ordinary text, so a stream is reversed by
    matching the exact values with a trie seeded from this session's ``mapping``.
    Everything else restores by codec syntax (pseudonym, hash) — the default.
    Read via ``getattr`` so any operator (including a plugin that predates the
    attribute) defaults to the codec path.
    """
    if getattr(operator, "stream_restore", "token") == "literal":
        return lambda: StreamingDeanonymizer(mapping)
    return lambda: TokenStreamProcessor(codec, resolve)


def _loads(data: str) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _index_of(payload: dict[str, Any]) -> int:
    index = payload.get("index", 0)
    return index if isinstance(index, int) else 0


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True
