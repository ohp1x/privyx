"""Streaming SSE transformer shared by the gateway and the transparent proxy.

:class:`StreamRouter` turns a stream of raw upstream chunks into a stream of
deanonymized SSE frames.  It is the one place that reconciles three buffers whose
boundaries never line up — bytes (a UTF-8 char may split across chunks), SSE
events (an event may split across chunks), and tokens (a pseudonym may split
across events) — and adds two more concerns on top:

* **Held-back text ordering.** The deanonymizer holds a trailing fragment that
  might still grow into a token; that fragment is flushed *before* any pass-through
  event and at end of stream, never after, so nothing arrives out of order.
* **Tool-call buffering.** OpenAI ``tool_calls`` / ``function_call``, Anthropic
  ``tool_use``, and Responses ``function_call_arguments`` (and kin) stream their
  arguments as partial JSON across many events.  Forwarding the partials would
  leak pseudonyms and fragment the JSON, so they are accumulated in pseudonym
  space and emitted — deanonymized, in one frame — only once complete.

Only deltas need that per-event care (a token may split across them).  Every
other event — ``message_start``, ``content_block_start``, ``citations_delta``,
Responses ``*.done`` / ``output_item.done`` / ``response.completed``, … — carries
complete strings, so its JSON is restored leaf by leaf with the same walk as a
batch body (:func:`~privyx.proxy.schemas.walk_sync`) and re-serialized only when
something changed.

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
from dataclasses import dataclass, replace
from typing import Any

from privyx.proxy.schemas import remember_thinking, walk_sync
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.deanonymizer import (
    StreamDeanonymizer,
    StreamingDeanonymizer,
    TokenStreamProcessor,
)
from privyx.streaming.sse import SSEDecoder, SSEEvent
from privyx.token.codec import TokenCodec

_DONE = "[DONE]"

#: OpenAI Chat delta fields streamed as text, each on its own deanonymizer.
#: ``reasoning`` is the non-standard field OpenRouter / vLLM / Ollama send.
_OA_TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")
#: ``_oa_tools`` slot for the legacy single ``delta.function_call``.
_LEGACY_CALL = -1

#: Responses delta kinds carrying tool-call input: buffered until ``.done``.
_RS_BUFFERED = frozenset(
    {
        "response.function_call_arguments",
        "response.mcp_call_arguments",
        "response.custom_tool_call_input",
    }
)
#: Responses events that end the stream: every held-back delta is flushed first.
_RS_TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed", "error"})
#: Fields that, with the event kind, identify one Responses delta stream.
_RS_INDEXES = ("item_id", "output_index", "content_index", "summary_index", "command_index")


@dataclass(slots=True)
class _Delta:
    """One Responses delta stream: its latest event (the re-wrap template), and
    a text deanonymizer — or ``None`` when tool input is buffered instead."""

    event: SSEEvent
    payload: dict[str, Any]
    processor: StreamDeanonymizer | None
    buffer: str = ""


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
        # Reused for complete strings: feed + flush always leaves it empty.
        self._full = self._make_processor()

        # Templates for re-wrapping a flushed tail into the provider's schema.
        self._text_template: SSEEvent | None = None
        self._think_index: int | None = None  # Anthropic

        self._pending_terminal: list[str] = []
        # OpenAI: delta text field -> its deanonymizer and latest chunk (template).
        self._oa_fields: dict[str, StreamDeanonymizer] = {}
        self._oa_templates: dict[str, dict[str, Any]] = {}
        # OpenAI: tool-call index -> {"id", "name", "args"} (pseudonym space).
        self._oa_tools: dict[int, dict[str, str]] = {}
        # Anthropic: content-block index -> accumulated partial_json.
        self._an_tools: dict[int, str] = {}
        # Anthropic: content-block index -> thinking text as sent (for its signature).
        self._an_thinking: dict[int, str] = {}
        self._block_types: dict[int, str] = {}
        # Responses: (kind, *indexes) -> its delta stream.
        self._rs: dict[tuple[Any, ...], _Delta] = {}

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
        reasoning = self._flush_reasoning_tail()
        if reasoning:
            out.append(reasoning)
        out.extend(self._flush_openai_tools())
        out.extend(self._flush_anthropic_tools())
        out.extend(self._flush_responses())
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
        if self._schema == "responses":
            return self._handle_responses(event, payload)
        return self._handle_openai(event, payload)

    # -- primary text (generic + Anthropic text_delta) ---------------------

    def _text_via_adapter(self, event: SSEEvent) -> list[str]:
        delta = self._adapter.extract_delta(event)
        if not delta:
            return self._passthrough(event)
        self._text_template = event
        out = self._text.feed(delta)
        return [self._adapter.wrap_delta(out, event).to_str()] if out else []

    def _flush_text_tail(self) -> list[str]:
        """Held-back visible text: the primary text buffer and OpenAI's fields."""
        frames: list[str] = []
        tail = self._text.flush()
        if tail and self._text_template is not None:
            frames.append(self._adapter.wrap_delta(tail, self._text_template).to_str())
        for field, processor in self._oa_fields.items():
            tail = processor.flush()
            if tail:
                frames.append(self._openai_field_frame(field, tail))
        return frames

    def _passthrough(self, event: SSEEvent, *, drain: bool = True) -> list[str]:
        """Forward an event that is not restored delta by delta.

        Its complete strings are restored in place (:meth:`_restore_event`).
        ``drain`` flushes any held-back text first so ordering is preserved; it
        is disabled for Anthropic control events (e.g. ``ping``) that may arrive
        *between* the chunks of a single token, where flushing would split it.
        """
        out = self._flush_text_tail() if drain else []
        out.append(self._restore_event(event).to_str())
        return out

    # -- OpenAI ------------------------------------------------------------

    def _handle_openai(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return self._passthrough(event)
        choice = choices[0]
        delta = choice.get("delta")
        delta = delta if isinstance(delta, dict) else {}
        finishing = bool(choice.get("finish_reason"))

        tool_calls = delta.get("tool_calls")
        function_call = delta.get("function_call")
        if isinstance(tool_calls, list) or isinstance(function_call, dict):
            if isinstance(tool_calls, list):
                self._accumulate_openai_tools(tool_calls)
            if isinstance(function_call, dict):
                self._accumulate_openai_tools([{"index": _LEGACY_CALL, "function": function_call}])
            if finishing:
                return self._flush_openai_tools() + [self._openai_finish_frame(payload)]
            return []  # still accumulating — suppress the partial

        if finishing and self._oa_tools:
            # Tools closed by a separate finish chunk carrying no tool_calls.
            return self._flush_openai_tools() + self._passthrough(event)

        fields = [f for f in _OA_TEXT_FIELDS if isinstance(delta.get(f), str) and delta[f]]
        if not fields:
            return self._passthrough(event)  # role-only / usage / finish

        # The rest of the chunk (e.g. OpenRouter ``reasoning_details``) carries
        # complete strings: restore them whole, then splice in the text fields.
        new = walk_sync(payload, self._restore_full)
        out_delta = new["choices"][0]["delta"]
        for field in fields:
            processor = self._oa_fields.get(field)
            if processor is None:
                processor = self._oa_fields[field] = self._make_processor()
            self._oa_templates[field] = payload
            # A finishing chunk closes the field: nothing may follow it.
            text = processor.feed(delta[field]) + (processor.flush() if finishing else "")
            if text:
                out_delta[field] = text
            else:
                del out_delta[field]
        if not out_delta and not finishing:
            return []  # everything held back
        return [_reframe(event, new)]

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
            restored = self._restore_arguments(args)
            if not _is_valid_json(restored):
                continue
            slot = self._oa_tools[index]
            frames.append(self._openai_tool_frame(index, slot["id"], slot["name"], restored))
        self._oa_tools = {}
        return frames

    def _flush_reasoning_tail(self) -> str | None:
        """An Anthropic thinking block the upstream never closed."""
        tail = self._think.flush()
        if not tail or self._think_index is None:
            return None
        return self._anthropic_delta_frame(self._think_index, "thinking_delta", "thinking", tail)

    def _openai_field_frame(self, field: str, text: str) -> str:
        """``text`` as the only delta field of ``field``'s latest chunk."""
        new = copy.deepcopy(self._oa_templates[field])
        new["choices"][0]["delta"] = {field: text}
        return SSEEvent(data=json.dumps(new, separators=(",", ":"))).to_str()

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
        function = {"name": name, "arguments": arguments}
        delta: dict[str, Any] = (
            {"function_call": function}
            if index == _LEGACY_CALL
            else {
                "tool_calls": [
                    {"index": index, "id": call_id, "type": "function", "function": function}
                ]
            }
        )
        payload = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta}]}
        return SSEEvent(data=json.dumps(payload, separators=(",", ":"))).to_str()

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
            remember_thinking(delta.get("signature"), text)

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

    # -- OpenAI Responses --------------------------------------------------

    def _handle_responses(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
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
        # ponytail: object deltas (shell_call_output_content's stdout/stderr) are
        # restored per event, so a token split across two of them reaches the
        # client as fragments (its .done is whole); give them processors if seen.
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
        return _reframe(slot.event, {**slot.payload, "delta": text})

    # -- shared ------------------------------------------------------------

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
        payload = _loads(event.data)
        if payload is None:
            return event
        restored = walk_sync(payload, self._restore_full)
        if restored == payload:
            return event
        return replace(event, data=_dumps(restored))


def resolver_for(operator: object, mapping: dict[str, str]) -> Callable[[str], str | None]:
    """Build the token → original resolver for ``operator`` over ``mapping``.

    Most operators store the plaintext in the mapping, so the resolver is just
    ``mapping.get``.  An operator whose mapping value is not the plaintext
    (``encrypt`` stores ciphertext) overrides
    :meth:`~privyx.privacy.operator.base.BaseOperator.build_resolver` to transform
    it.  Read via ``getattr`` so a plugin predating the method still resolves.
    """
    builder = getattr(operator, "build_resolver", None)
    if callable(builder):
        resolver: Callable[[str], str | None] = builder(mapping)
        return resolver
    return mapping.get


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
    # A token's id may be written alone.  The session's tokens are indexed once
    # for all of this stream's processors, and again on its next stream: keep
    # the index with the session if a long one ever makes that show.
    index = getattr(codec, "ids", None)
    ids = index(mapping) if index else None
    return lambda: TokenStreamProcessor(codec, resolve, ids)


def _reframe(event: SSEEvent, payload: dict[str, Any]) -> str:
    """``payload`` serialized into ``event``'s envelope (keeps ``event:``/``id:``)."""
    return replace(event, data=_dumps(payload)).to_str()


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
