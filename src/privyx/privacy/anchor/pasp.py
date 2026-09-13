"""PASP anchor — Pseudonymization with Attribute-Specific Pseudonyms.

Placeholder for a more advanced anchor scheme (e.g. per-attribute keys,
blinding, or format-preserving pseudonyms).  Kept as a stub so the module
layout is complete and the concept is documented.
"""

from __future__ import annotations

from privyx.core.context import Context
from privyx.privacy.anchor.base import BaseAnchor


class PASAnchor(BaseAnchor):
    """Attribute-specific pseudonym anchor (placeholder).

    NOTE: Not yet implemented.  Use :class:`HMACAnchor` for keyed pseudonyms.
    """

    name = "pasp"

    async def anchor(self, text: str, context: Context) -> str:
        raise NotImplementedError("PASAnchor is not yet implemented")

    async def deanchor(self, anchored: str, context: Context) -> str | None:
        raise NotImplementedError("PASAnchor is not yet implemented")