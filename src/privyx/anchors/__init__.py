"""Anchors: derive a pseudonym's id from the value and a key."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.anchors._base import Anchor
from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.anchors._hmac import HMACAnchor

__all__ = ["Anchor", "HMACAnchor"]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "hmac": "privyx.anchors._hmac:HMACAnchor",
    "pasp": "privyx.anchors._hmac:PASPAnchor",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.anchors import HMACAnchor`` imports that module on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in anchors with ``app``."""
    app.registry(Anchor).builtins(_BUILTINS)
