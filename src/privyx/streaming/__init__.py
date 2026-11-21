"""Streaming restore.

The streaming layer guarantees that chunked output, pseudonymized at the
boundary, is restored correctly even when a pseudonym is split across chunks.
SSE envelopes are separated from text transformation: an API format's dialect
reads its own events (:class:`StreamRouter` subclasses).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx.streaming._router import DONE as DONE
    from privyx.streaming._router import (
        LineRouter,
        ObjectStream,
        StreamRouter,
        resolver_for,
        restore_chunks,
        select_processor_factory,
    )
    from privyx.streaming._router import dumps as dumps
    from privyx.streaming._router import is_valid_json as is_valid_json
    from privyx.streaming._router import loads as loads
    from privyx.streaming._router import reframe as reframe
    from privyx.streaming._sse_adapter import SSEStreamAdapter
    from privyx.streaming.deanonymizer import (
        StreamDeanonymizer,
        StreamingDeanonymizer,
        TokenStreamProcessor,
    )
    from privyx.streaming.sse import SSEDecoder, SSEEvent

__all__ = [
    "LineRouter",
    "ObjectStream",
    "SSEDecoder",
    "SSEEvent",
    "SSEStreamAdapter",
    "StreamDeanonymizer",
    "StreamRouter",
    "StreamingDeanonymizer",
    "TokenStreamProcessor",
    "resolver_for",
    "restore_chunks",
    "select_processor_factory",
]

_ROUTER = "privyx.streaming._router"
#: Each name, imported on first use: masking without a stream needs none of them.
_LAZY = {
    **{
        name: f"{_ROUTER}:{name}"
        for name in (
            "DONE",
            "LineRouter",
            "ObjectStream",
            "StreamRouter",
            "dumps",
            "is_valid_json",
            "loads",
            "reframe",
            "resolver_for",
            "restore_chunks",
            "select_processor_factory",
        )
    },
    "SSEStreamAdapter": "privyx.streaming._sse_adapter:SSEStreamAdapter",
    **{
        name: f"privyx.streaming.deanonymizer:{name}"
        for name in ("StreamDeanonymizer", "StreamingDeanonymizer", "TokenStreamProcessor")
    },
    "SSEDecoder": "privyx.streaming.sse:SSEDecoder",
    "SSEEvent": "privyx.streaming.sse:SSEEvent",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.streaming import StreamRouter`` imports it on first use."""
    return lazy_attribute(name, _LAZY, __name__)
