"""Privyx token subsystem.

The single source of truth for how a token looks in text.  Everything else in
Privyx works with :class:`LogicalToken` — a namespace/type/identifier triple —
and delegates serialization to a :class:`FormatCodec`.
"""

from __future__ import annotations

from privyx.token._build import build_codec
from privyx.token.codec import FormatCodec, TokenMatch
from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

__all__ = [
    "FormatCodec",
    "LogicalToken",
    "TokenFormatError",
    "TokenMatch",
    "build_codec",
]
