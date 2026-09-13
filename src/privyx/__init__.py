"""Privyx — AI data privacy gateway.

Core public API. Import the privacy engine, sessions, and vault backends here.
"""

from __future__ import annotations

from privyx.core.engine import PrivacyEngine
from privyx.core.session import Session
from privyx.version import __version__

__all__ = ["PrivacyEngine", "Session", "__version__"]
