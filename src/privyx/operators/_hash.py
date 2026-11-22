"""Hash-based operator — replaces spans with a content-addressable token.

WARNING: Not truly reversible unless you maintain a lookup table (which is
what the session vault does).  If used without a vault, this is one-way.
"""

from __future__ import annotations

import hashlib
from functools import partial
from typing import Any

from pydantic import Field

from privyx.core import (
    Context,
    Detection,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._restore import TokenOperator, free_token


class HashOperator(TokenOperator):
    """Replace sensitive spans with a token whose identifier is a truncated hash.

    The hash is deterministic (same input → same identifier), so it can act as a
    pseudonym across sessions, but it is **not** reversible in the general case.
    Deanonymization looks up the original text in the session mapping (which must
    have been populated during pseudonymization).  ``id_length`` hex characters
    of the SHA-256 digest are kept; a collision with another value of the session
    lengthens the id.  Tokens are written in the one configured token syntax.
    """

    name = "hash"
    uses_anchor = False

    class Options(TokenOperator.Options):
        id_length: int = Field(default=12, ge=6, le=64)  # hex characters of the digest kept

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._length = self.options.id_length

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
            pseudo = free_token(
                session,
                span.text,
                hashlib.sha256(span.text.encode()).hexdigest(),
                self._length,
                partial(self._encode, span.entity_type),
            )
            session.put(pseudo, span.text)
            transforms.append(
                Transformation(span.start, span.end, span.text, pseudo, span.entity_type)
            )
        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )
