"""Privyx — AI data privacy gateway.

The public API, each name imported on first use (PEP 562), so ``import
privyx`` and ``privyx --help`` load neither pydantic nor the web stack.
:class:`Privyx` (experimental) assembles and serves Privyx from Python,
:func:`load_config` reads the settings, and :class:`PrivacyEngine` masks and
restores text.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from privyx.version import __version__

if TYPE_CHECKING:
    from privyx.app import Privyx, load_config
    from privyx.config.schema import Settings
    from privyx.core import ConfigError, PrivyxError, Session
    from privyx.engine import PrivacyEngine

__all__ = [
    "ConfigError",
    "PrivacyEngine",
    "Privyx",
    "PrivyxError",
    "Session",
    "Settings",
    "__version__",
    "load_config",
]

_LAZY = {
    "Privyx": "privyx.app",
    "load_config": "privyx.app",
    "Settings": "privyx.config.schema",
    "PrivacyEngine": "privyx.engine",
    "Session": "privyx.core",
    "PrivyxError": "privyx.core",
    "ConfigError": "privyx.core",
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(_LAZY[name]), name)
    globals()[name] = value
    return value
