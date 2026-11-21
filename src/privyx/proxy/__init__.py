"""The privacy proxy: the exchange flow, its routing, and its ASGI adapter.

Loaded on first use (PEP 562), so the formats in :mod:`privyx.proxy.schemas`
can be read without the HTTP stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx.proxy._pipeline import Pipeline, Reply
    from privyx.proxy._upstream import served_routes
    from privyx.proxy.stream_router import StreamRouter

__all__ = ["Pipeline", "Reply", "StreamRouter", "served_routes"]

_LAZY = {
    "Pipeline": "privyx.proxy._pipeline:Pipeline",
    "Reply": "privyx.proxy._pipeline:Reply",
    "served_routes": "privyx.proxy._upstream:served_routes",
    "StreamRouter": "privyx.proxy.stream_router:StreamRouter",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.proxy import Pipeline`` imports it on first use."""
    return lazy_attribute(name, _LAZY, __name__)
