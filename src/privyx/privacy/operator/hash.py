"""Hash-based operator — replaces spans with a content-addressable token.

WARNING: Not truly reversible unless you maintain a lookup table (which is
what the session vault does).  If used without a vault, this is one-way.
"""

from __future__ import annotations

import hashlib

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator, restore
from privyx.privacy.transform.text import apply_replacements
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.token.model import LogicalToken


class HashOperator(BaseOperator):
    """Replace sensitive spans with a token whose identifier is a truncated hash.

    The hash is deterministic (same input → same identifier), so it can act as a
    pseudonym across sessions, but it is **not** reversible in the general case.
    Deanonymization looks up the original text in the session mapping (which must
    have been populated during pseudonymization).

    Args:
        length: Number of hex characters of the SHA-256 digest to keep.
        codec: Token codec deciding how tokens are serialized.  Defaults to the
            built-in :meth:`~privyx.token.codec.FormatCodec.default` syntax, so
            hash tokens share the one configured syntax rather than inventing
            their own.
    """

    name = "hash"

    def __init__(self, length: int = 12, codec: TokenCodec | None = None) -> None:
        self._length = length
        self._codec = codec or FormatCodec.default()

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)
        transforms: list[Transformation] = []
        for span in detection.spans:
            digest = hashlib.sha256(span.text.encode()).hexdigest()[: self._length]
            pseudo = self._codec.encode(
                LogicalToken(
                    namespace=self._codec.namespace,
                    type=span.entity_type,
                    identifier=digest,
                )
            )
            session.put(pseudo, span.text)
            transforms.append(Transformation(span.start, span.end, span.text, pseudo))
        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore tokens that this session issued.

        The hash itself is one-way; the session mapping is what makes this
        reversible.  A token the session has never seen is left as-is.
        """
        return restore(text, session, self._codec)
