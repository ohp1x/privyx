"""Anthropic message-stream adapter.

Anthropic's streaming API emits ``content_block_delta`` events with
``delta.text`` payloads.  Non-text deltas (e.g. ``thinking``, ``citations``)
are passed through unchanged.
"""

from __future__ import annotations

import json

from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.sse import SSEEvent


class AnthropicStreamAdapter(SSEStreamAdapter):
    """Adapter for Anthropic-style SSE streaming.

    Only ``content_block_delta`` events with ``delta.type == "text_delta"``
    are transformed; everything else passes through untouched.
    """

    schema_name = "anthropic"

    def __init__(self) -> None:
        super().__init__(data_path=None)  # no default path; we inspect events

    def extract_delta(self, event: SSEEvent) -> str:
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError:
            return ""
        if payload.get("type") != "content_block_delta":
            return ""
        delta = payload.get("delta", {})
        if delta.get("type") != "text_delta":
            return ""
        text = delta.get("text", "")
        return text if isinstance(text, str) else ""

    def wrap_delta(self, delta: str, original: SSEEvent | None = None) -> SSEEvent:
        if original is None:
            return SSEEvent(data=delta)
        try:
            payload = json.loads(original.data)
        except json.JSONDecodeError:
            return original
        if payload.get("type") != "content_block_delta":
            return original
        delta_payload = payload.get("delta", {})
        if delta_payload.get("type") != "text_delta":
            return original
        payload["delta"]["text"] = delta
        return SSEEvent(
            data=json.dumps(payload, separators=(",", ":")),
            event=original.event,
            id=original.id,
            retry=original.retry,
        )