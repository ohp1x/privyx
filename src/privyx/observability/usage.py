"""Token usage the upstream reports, and what it costs.

Providers report ``usage`` in three shapes: Anthropic Messages (input without
the cached part, plus ``cache_read_input_tokens`` and
``cache_creation_input_tokens``), OpenAI Chat Completions (``prompt_tokens``
with the cached part inside, ``prompt_tokens_details.cached_tokens``), and
OpenAI Responses (``input_tokens`` with ``input_tokens_details.cached_tokens``).
:func:`normalize` turns any of them into the four counts the audit trail
keeps: ``input`` (uncached), ``output``, ``cache_read``, and ``cache_write``.

Usage holds counts only.  A model id comes from the upstream, so it is kept
only when it looks like one and the built-in patterns find nothing in it
(:func:`safe_model`): an email, a key, or an address there is dropped.
"""

from __future__ import annotations

import codecs
import functools
import json
import re
from collections.abc import Mapping
from typing import Any

#: Usage fields summed when a reply gives no ``total_tokens``.
_COUNTED = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "prompt_tokens",
    "completion_tokens",
)
#: Nested counts kept under their own name: the cached part of the input.
_DETAILS = ("prompt_tokens_details", "input_tokens_details")
_MODEL = re.compile(r"[\w.:/@-]{1,128}")

#: Built-in prices in USD per million tokens: input, output, cache read, cache
#: write (5-minute).  Matched by the longest model-id prefix; ``audit.prices``
#: adds models or overrides these.  Estimates: check your provider's price list.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}


def merge_usage(fields: dict[str, int], event: Any) -> str | None:
    """Fold the ``usage`` of one reply or stream event into ``fields``.

    A stream reports usage more than once (Anthropic's ``message_start`` and
    ``message_delta``), each time the running count, so later values replace
    earlier ones.  Returns the model id the event names, if any.
    """
    if not isinstance(event, dict):
        return None
    model = None
    for holder in (event, event.get("message"), event.get("response")):
        if not isinstance(holder, dict):
            continue
        name = holder.get("model")
        if isinstance(name, str) and safe_model(name):
            model = name
        usage = holder.get("usage")
        if isinstance(usage, dict):
            fields.update((k, v) for k, v in usage.items() if _count(v))
            for key in _DETAILS:
                details = usage.get(key)
                if isinstance(details, dict) and _count(details.get("cached_tokens")):
                    fields["cached_tokens"] = details["cached_tokens"]
    return model


@functools.lru_cache(maxsize=256)
def safe_model(name: str) -> bool:
    """Whether ``name`` may enter the audit trail as a model id.

    It must look like one, and the built-in detector must find nothing in it.
    """
    if not _MODEL.fullmatch(name):
        return False
    from privyx.core.context import Context

    return not _detector().detect_sync(name, Context()).spans


@functools.cache
def _detector() -> Any:
    from privyx.privacy.detector.builtin import RegexDetector

    return RegexDetector()


def usage_tokens(fields: Mapping[str, int]) -> int:
    """The tokens a reply's merged ``usage`` fields add up to."""
    return fields.get("total_tokens") or sum(fields.get(key, 0) for key in _COUNTED)


def normalize(fields: Mapping[str, int]) -> dict[str, int]:
    """``input`` (uncached), ``output``, ``cache_read``, ``cache_write`` from merged fields.

    OpenAI counts the cached part inside its input; Anthropic does not.
    """
    cached = fields.get("cached_tokens", 0)
    total_input = fields.get("prompt_tokens", fields.get("input_tokens", 0))
    return {
        "input": max(total_input - cached, 0),
        "output": fields.get("completion_tokens", fields.get("output_tokens", 0)),
        "cache_read": fields.get("cache_read_input_tokens", 0) + cached,
        "cache_write": fields.get("cache_creation_input_tokens", 0),
    }


def cache_hit_rate(usage: Mapping[str, int]) -> float | None:
    """The share of input tokens read from the prompt cache, or ``None`` without input."""
    total = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
    return usage.get("cache_read", 0) / total if total else None


def cost(
    model: str, usage: Mapping[str, int], prices: Mapping[str, Any] | None = None
) -> float | None:
    """Estimated USD cost of ``usage`` on ``model``, or ``None`` for an unknown model.

    ``prices`` (``audit.prices``) are checked before :data:`PRICES`; each entry
    has ``input`` and ``output``, and optionally ``cache_read`` (default a
    tenth of input) and ``cache_write`` (default 1.25 times input).
    """
    rates = None
    for table in (prices or {}, PRICES):
        match = max((p for p in table if model.startswith(p)), key=len, default=None)
        if match is not None:
            rates = _rates(table[match])
            break
    if rates is None:
        return None
    keys = ("input", "output", "cache_read", "cache_write")
    return sum(usage.get(k, 0) * rate for k, rate in zip(keys, rates, strict=True)) / 1e6


def _rates(entry: Any) -> tuple[float, float, float, float]:
    if isinstance(entry, tuple):
        return entry
    data = entry if isinstance(entry, Mapping) else entry.model_dump()
    read = data.get("cache_read")
    write = data.get("cache_write")
    return (
        data["input"],
        data["output"],
        data["input"] * 0.1 if read is None else read,
        data["input"] * 1.25 if write is None else write,
    )


class UsageMeter:
    """Collect the usage and model of one reply, batch or streamed.

    Feed a batch body to :meth:`feed_json`, or an SSE stream in any chunking
    and line endings to :meth:`feed_sse`; then :attr:`usage` holds the
    normalized counts, or ``None`` when the reply reported none.
    """

    def __init__(self) -> None:
        from privyx.streaming.sse import SSEDecoder

        self.fields: dict[str, int] = {}
        self.model: str | None = None
        self._events = SSEDecoder()
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")

    def feed_json(self, payload: Any) -> None:
        self.model = merge_usage(self.fields, payload) or self.model

    def feed_sse(self, chunk: bytes | str) -> None:
        text = chunk if isinstance(chunk, str) else self._utf8.decode(chunk)
        self._read(self._events.feed(text))

    def close(self) -> None:
        """Read a last event that ended without a blank line."""
        self._read([*self._events.feed(self._utf8.decode(b"", final=True)), *self._events.flush()])

    def _read(self, events: list[Any]) -> None:
        for event in events:
            # Most events carry no usage; parse only those that may.
            if '"usage"' in event.data or '"model"' in event.data:
                self.feed_json(_loads(event.data))

    @property
    def usage(self) -> dict[str, int] | None:
        return normalize(self.fields) if self.fields else None


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _loads(data: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None
