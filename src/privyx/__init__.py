"""Privyx — AI data privacy gateway.

Core public API. Import the privacy engine, sessions, and vault backends here.
:class:`Privyx` (experimental) assembles and serves Privyx from Python; it is
loaded on first use, so importing this package does not import the web stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import Session
from privyx.engine import PrivacyEngine
from privyx.version import __version__

if TYPE_CHECKING:
    from privyx.app import Privyx

__all__ = ["PrivacyEngine", "Privyx", "Session", "__version__"]


def __getattr__(name: str) -> Any:
    if name == "Privyx":
        from privyx.app import Privyx

        return Privyx
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
