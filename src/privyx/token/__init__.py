"""Privyx token subsystem.

The single source of truth for how a token looks in text.  Everything else in
Privyx works with :class:`LogicalToken` — a namespace/type/identifier triple —
and delegates serialization to a :class:`TokenCodec`.  See
``.temp/token-system.md`` for the design contract.
"""

from __future__ import annotations

from privyx.token.codec import FormatCodec, TokenCodec, TokenMatch
from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

__all__ = [
    "FormatCodec",
    "LogicalToken",
    "TokenCodec",
    "TokenFormatError",
    "TokenMatch",
]
