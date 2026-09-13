"""Generic streaming adapter — plain SSE passthrough."""

from __future__ import annotations

from typing import Any

from privyx.streaming.sse import SSEEvent


class SSEStreamAdapter:
    """Transforms raw SSE text into text deltas and back.

    This adapter understands the ``data:`` payload only.  It is the default
    for any provider that speaks SSE.
    """

    def __init__(self, data_path: list[str] | None = None) -> None:
        # If the data payload is JSON, optionally extract a nested field
        # (e.g. ["choices", "0", "delta", "content"] for OpenAI).
        self._data_path = data_path

    def extract_delta(self, event: SSEEvent) -> str:
        """Extract the text delta from an SSE event."""
        if self._data_path is None:
            return event.data
        try:
            import json

            payload = json.loads(event.data)
        except json.JSONDecodeError:
            return ""
        node: Any = payload
        for key in self._data_path:
            if isinstance(node, dict):
                node = node.get(key)
            elif isinstance(node, list):
                try:
                    node = node[int(key)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
        return node if isinstance(node, str) else ""

    def wrap_delta(self, delta: str, original: SSEEvent | None = None) -> SSEEvent:
        """Re-wrap a transformed delta into an SSE event.

        If ``original`` is provided and the data is JSON, the transformed
        delta is spliced back into the original payload.
        """
        if original is None or self._data_path is None:
            return SSEEvent(data=delta, event=original.event if original else None)
        try:
            import json

            payload = json.loads(original.data)
        except json.JSONDecodeError:
            return SSEEvent(data=delta, event=original.event)
        node: Any = payload
        for key in self._data_path[:-1]:
            if isinstance(node, dict):
                node = node.get(key)
            elif isinstance(node, list):
                try:
                    node = node[int(key)]
                except (ValueError, IndexError):
                    return SSEEvent(data=delta, event=original.event)
        node[self._data_path[-1]] = delta
        return SSEEvent(
            data=json.dumps(payload, separators=(",", ":")),
            event=original.event,
            id=original.id,
            retry=original.retry,
        )