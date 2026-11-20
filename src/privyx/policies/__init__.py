"""Policies: decide which detections to act on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.policies._base import Policy

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.policies._default import DefaultPolicy
    from privyx.policies._strict import StrictPolicy

__all__ = ["DefaultPolicy", "Policy", "StrictPolicy"]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "default": "privyx.policies._default:DefaultPolicy",
    "strict": "privyx.policies._strict:StrictPolicy",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.policies import StrictPolicy`` imports that module on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in policies with ``app``."""
    app.registry(Policy).builtins(_BUILTINS)
