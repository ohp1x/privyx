"""The ``openai`` dialect: OpenAI Chat Completions, and the legacy completions."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from privyx.core import walk_sync
from privyx.dialects._base import CONTENT_KEYS, PREFIX_KEYS, Dialect
from privyx.streaming import (
    SSEEvent,
    StreamDeanonymizer,
    StreamRouter,
    dumps,
    is_valid_json,
    loads,
    reframe,
)

if TYPE_CHECKING:
    from privyx.token import FormatCodec

#: OpenAI Chat delta fields streamed as text, each on its own deanonymizer.
#: ``reasoning`` is the non-standard field OpenRouter / vLLM / Ollama send.
_OA_TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")
#: ``_oa_tools`` slot for the legacy single ``delta.function_call``.
_LEGACY_CALL = -1


class OpenAIDialect(Dialect):
    """OpenAI Chat Completions and the legacy completions, also as other servers speak them."""

    name = "openai"
    content_keys = CONTENT_KEYS
    prefix_keys = PREFIX_KEYS

    def ask_usage(self, payload: dict[str, Any]) -> None:
        """A streamed reply carries ``usage`` only when the request asks with ``include_usage``.

        A request that says ``include_usage`` itself is left as it is.
        """
        options = payload.get("stream_options")
        if payload.get("stream") is True and (
            options is None or (isinstance(options, dict) and "include_usage" not in options)
        ):
            payload["stream_options"] = {**(options or {}), "include_usage": True}

    def add_directive(self, payload: dict[str, Any], text: str, path: str) -> None:
        """After the leading system messages, where it stays put turn after turn."""
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        at = 0
        while at < len(messages) and _role(messages[at]) in ("system", "developer"):
            at += 1
        messages.insert(at, {"role": "system", "content": text})

    def stream(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer],
    ) -> StreamRouter:
        return OpenAIStream(codec, resolve, make_processor)


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else None


class OpenAIStream(StreamRouter):
    """A Chat Completions stream: text fields, and tool calls buffered until whole.

    ``tool_calls`` and ``function_call`` stream their arguments as partial JSON
    across many chunks.  Forwarding the partials would leak pseudonyms and
    fragment the JSON, so they are accumulated in pseudonym space and emitted,
    restored, in one frame once complete.  The ``[DONE]`` sentinel goes last.
    """

    def __init__(
        self,
        codec: FormatCodec,
        resolve: Callable[[str], str | None],
        make_processor: Callable[[], StreamDeanonymizer] | None = None,
    ) -> None:
        super().__init__(codec, resolve, make_processor)
        # Delta text field -> its deanonymizer and latest chunk (template).
        self._oa_fields: dict[str, StreamDeanonymizer] = {}
        self._oa_templates: dict[str, dict[str, Any]] = {}
        # Tool-call index -> {"id", "name", "args"} (pseudonym space).
        self._oa_tools: dict[int, dict[str, str]] = {}

    def _event(self, event: SSEEvent) -> list[str]:
        payload = loads(event.data)
        if payload is None:
            return self._passthrough(event)
        return self._json(event, payload)

    def _flush_text_tail(self) -> list[str]:
        frames = super()._flush_text_tail()
        for field, processor in self._oa_fields.items():
            tail = processor.flush()
            if tail:
                frames.append(self._openai_field_frame(field, tail))
        return frames

    def _flush_held(self) -> list[str]:
        return self._flush_openai_tools()

    def _json(self, event: SSEEvent, payload: dict[str, Any]) -> list[str]:
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

        is_legacy = any(isinstance(c, dict) and "text" in c and "delta" not in c for c in choices)
        if is_legacy:
            new_choices = []
            has_output = False
            for c in choices:
                if not isinstance(c, dict) or "text" not in c:
                    new_choices.append(c)
                    continue
                c_idx = c.get("index", 0)
                field_key = f"text_{c_idx}"
                processor = self._oa_fields.get(field_key)
                if processor is None:
                    processor = self._oa_fields[field_key] = self._make_processor()
                self._oa_templates[field_key] = payload
                c_finishing = bool(c.get("finish_reason"))
                text_val = c.get("text", "")
                text = processor.feed(text_val) + (processor.flush() if c_finishing else "")
                new_c = copy.deepcopy(c)
                new_c["text"] = text
                if text or c_finishing:
                    has_output = True
                new_choices.append(new_c)
            if has_output:
                new = copy.deepcopy(payload)
                new["choices"] = new_choices
                return [reframe(event, new)]
            return []

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
        return [reframe(event, new)]

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
            if not is_valid_json(restored):
                continue
            slot = self._oa_tools[index]
            frames.append(self._openai_tool_frame(index, slot["id"], slot["name"], restored))
        self._oa_tools = {}
        return frames

    def _openai_field_frame(self, field: str, text: str) -> str:
        """``text`` as the only delta field of ``field``'s latest chunk."""
        new = copy.deepcopy(self._oa_templates[field])
        if field.startswith("text_"):
            choice_idx = int(field.split("_", 1)[1])
            target_choice: dict[str, Any] | None = None
            for c in new.get("choices", []):
                if isinstance(c, dict) and c.get("index") == choice_idx:
                    target_choice = copy.deepcopy(c)
                    break
            if target_choice is None:
                target_choice = {"index": choice_idx}
            target_choice["text"] = text
            new["choices"] = [target_choice]
        else:
            new["choices"][0]["delta"] = {field: text}
        return SSEEvent(data=dumps(new)).to_str()

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
