"""Hash-based operator — replaces spans with a content-addressable hash.

WARNING: Not truly reversible unless you maintain a lookup table (which is
what the session vault does).  If used without a vault, this is one-way.
"""

from __future__ import annotations

import hashlib
import re

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator, restore


class HashOperator(BaseOperator):
    """Replace sensitive spans with a truncated SHA-256 hash.

    The hash is deterministic (same input → same hash), so it can be used as a
    pseudonym across sessions, but it is **not** reversible in the general case.
    Deanonymization looks up the original text in the session mapping (which
    must have been populated during pseudonymization).
    """

    name = "hash"

    def __init__(self, prefix: str = "HASH_", length: int = 12) -> None:
        self._prefix = prefix
        self._length = length
        # Match this operator's own placeholders.  Delegating to the
        # pseudonym operator's ``<PRIVYX_...>`` pattern would never match a
        # ``HASH_...`` token, making deanonymization a silent no-op.
        self._pattern = re.compile(re.escape(prefix) + f"[0-9a-f]{{{length}}}")

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)
        result_text = text
        transforms: list[Transformation] = []
        for span in reversed(detection.spans):
            h = hashlib.sha256(span.text.encode()).hexdigest()[: self._length]
            pseudo = f"{self._prefix}{h}"
            session.put(pseudo, span.text)
            result_text = result_text[: span.start] + pseudo + result_text[span.end :]
            transforms.append(Transformation(span.start, span.end, span.text, pseudo))
        transforms.reverse()
        return TransformResult(text=result_text, transformations=transforms)

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore hashes that this session issued.

        The hash itself is one-way; the session mapping is what makes this
        reversible.  A hash the session has never seen is left as-is.
        """
        return restore(text, session, self._pattern)