"""Pseudonymization operator using reversible tokens.

The operator works with :class:`~privyx.token.model.LogicalToken` values and
delegates their textual form to a :class:`~privyx.token.codec.TokenCodec`, so it
never mentions delimiters or a fixed syntax.  The
identifier is a per-session counter by default, or — when an
:class:`~privyx.privacy.anchor.base.Anchor` is supplied — a deterministic token
derived from the value itself, so the same input yields the same pseudonym in
every session.
"""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.anchor.base import Anchor
from privyx.privacy.operator.base import BaseOperator, restore
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.token.model import LogicalToken

#: How many hex characters of an anchor digest to keep.  16 hex chars = 64 bits
#: of the HMAC, far beyond collision range for a single conversation while
#: staying short enough to read in a log.
ANCHOR_TOKEN_LENGTH = 16


class PseudonymOperator(BaseOperator):
    """Replace detected spans with deterministic pseudonym tokens.

    Each unique original value maps to a consistent token via the session's
    ``reverse`` mapping.  New values get a fresh identifier: a per-session
    counter, or an anchor-derived token when ``anchor`` is given.

    Args:
        codec: Token codec deciding how tokens are serialized.  Defaults to the
            built-in :meth:`~privyx.token.codec.FormatCodec.default` syntax.
        anchor: Optional anchor making pseudonyms deterministic across sessions.
            The vault is still the source of truth for restoring them — an anchor
            is one-way — so this changes how pseudonyms are *named*, not how they
            are reversed.
    """

    name = "pseudonym"

    def __init__(
        self,
        codec: TokenCodec | None = None,
        anchor: Anchor | None = None,
    ) -> None:
        self._codec = codec or FormatCodec.default()
        self._anchor = anchor

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)

        # Assign pseudonyms in document order so counters read left to right,
        # then apply the replacements right to left so earlier offsets stay
        # valid as the text grows or shrinks.
        replacements: list[tuple[int, int, str, str]] = []
        for span in detection.spans:
            original = span.text
            pseudo = session.pseudonym_for(original) or await self._issue(
                span.entity_type, original, session, context
            )
            session.put(pseudo, original)
            replacements.append((span.start, span.end, original, pseudo))

        result_text = text
        for start, end, _, pseudo in reversed(replacements):
            result_text = result_text[:start] + pseudo + result_text[end:]

        transforms = [
            Transformation(start, end, original, pseudo)
            for start, end, original, pseudo in replacements
        ]
        return TransformResult(text=result_text, transformations=transforms)

    async def _issue(
        self, entity_type: str, original: str, session: Session, context: Context
    ) -> str:
        """Mint a fresh pseudonym token for ``entity_type``.

        With an anchor, the identifier is derived from the value, so the same
        input produces the same pseudonym in every session.  Without one, it is a
        per-session counter that probes past any value already taken: deleting a
        mapping entry makes ``len()`` go backwards, which would otherwise
        re-issue a pseudonym still bound to a different original.
        """
        if self._anchor is not None:
            anchored = await self._anchor.anchor(original, context)
            return self._encode(entity_type, self._token(anchored))

        counter = len(session.mapping) + 1
        pseudo = self._encode(entity_type, str(counter))
        while pseudo in session.mapping:
            counter += 1
            pseudo = self._encode(entity_type, str(counter))
        return pseudo

    def _encode(self, entity_type: str, identifier: str) -> str:
        return self._codec.encode(
            LogicalToken(
                namespace=self._codec.namespace,
                type=entity_type,
                identifier=identifier,
            )
        )

    @staticmethod
    def _token(anchored: str) -> str:
        """Reduce an anchor's output to a short uppercase hex identifier.

        Anchors return their own format (``PRIVYX_<64 hex>`` for HMAC); only the
        digest tail is kept, so the identifier stays readable and short.
        """
        digest = anchored.rsplit("_", 1)[-1]
        return digest[:ANCHOR_TOKEN_LENGTH].upper()

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore all tokens in ``text`` using the session mapping."""
        return restore(text, session, self._codec)
