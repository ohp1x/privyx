"""Features: optional behavior that listens to events, each switched on and off.

``audit`` and ``metrics`` are on unless switched off; ``tracing``,
``directive``, and ``cache_control`` when switched on; ``budget`` by a
``privyx run`` profile that sets one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute, listen
from privyx.features._base import Feature

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.features._audit import AuditTrail
    from privyx.features._budget import Budget
    from privyx.features._cache_control import CacheControl
    from privyx.features._directive import Directive
    from privyx.features._metrics import AuditStats, Metrics
    from privyx.features._tracing import Tracing

__all__ = [
    "AuditStats",
    "AuditTrail",
    "Budget",
    "CacheControl",
    "Directive",
    "Feature",
    "Metrics",
    "Tracing",
    "listen",
]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "audit": "privyx.features._audit:AuditTrail",
    "metrics": "privyx.features._metrics:Metrics",
    "tracing": "privyx.features._tracing:Tracing",
    "directive": "privyx.features._directive:Directive",
    "cache_control": "privyx.features._cache_control:CacheControl",
    "budget": "privyx.features._budget:Budget",
}

_LAZY = {**_BUILTINS, "stats": "privyx.features._metrics:AuditStats"}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.features import Budget`` imports that module on first use."""
    return lazy_attribute(name, _LAZY, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in features with ``app``."""
    app.registry(Feature).builtins(_BUILTINS)
