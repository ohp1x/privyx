"""The privacy proxy: the exchange flow, its routing, and its ASGI adapter.

Loaded on first use (PEP 562), so the routing helpers can be read without the
HTTP stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx.proxy._mask import transform_request as transform_request
    from privyx.proxy._pipeline import Pipeline, Reply
    from privyx.proxy._upstream import served_routes

__all__ = ["Pipeline", "Reply", "served_routes"]

_LAZY = {
    "Pipeline": "privyx.proxy._pipeline:Pipeline",
    "Reply": "privyx.proxy._pipeline:Reply",
    "served_routes": "privyx.proxy._upstream:served_routes",
    # Outside __all__: the request transform alone, which ``privyx doctor`` probes.
    "transform_request": "privyx.proxy._mask:transform_request",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.proxy import Pipeline`` imports it on first use."""
    return lazy_attribute(name, _LAZY, __name__)
