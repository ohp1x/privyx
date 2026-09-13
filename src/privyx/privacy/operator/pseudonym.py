"""Pseudonymization operator using reversible placeholders.

The pseudonym format is ``<PRIVYX_<entity>_<suffix>>`` so that deanonymization
can find and replace them in streaming text.  The suffix is a per-session
counter by default, or — when an :class:`~privyx.privacy.anchor.base.Anchor` is
supplied — a deterministic token derived from the value itself, so the same
input yields the same pseudonym in every session.

Entity types may themselves contain underscores (``CREDIT_CARD``,
``IP_ADDRESS``), so the pattern matches the whole ``[A-Z0-9_]+`` body rather
than assuming the entity type is underscore-free.
"""

from __future__ import annotations

import re

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.anchor.base import Anchor
from privyx.privacy.operator.base import BaseOperator, restore

_PSEUDO_PREFIX = "<PRIVYX_"
_PSEUDO_SUFFIX = ">"

#: How many hex characters of an anchor digest to keep.  16 hex chars = 64 bits
#: of the HMAC, far beyond collision range for a single conversation while
#: staying short enough to read in a log.
ANCHOR_TOKEN_LENGTH = 16


class PseudonymOperator(BaseOperator):
    """Replace detected spans with deterministic pseudonyms.

    Each unique original value maps to a consistent pseudonym via the session's
    ``reverse`` mapping.  New values get a fresh pseudonym: a per-session
    counter, or an anchor-derived token when ``anchor`` is given.

    Args:
        prefix: Opening delimiter of the placeholder.
        suffix: Closing delimiter of the placeholder.
        anchor: Optional anchor making pseudonyms deterministic across
            sessions.  The vault is still the source of truth for restoring
            them — an anchor is one-way — so this changes how pseudonyms are
            *named*, not how they are reversed.
    """

    name = "pseudonym"

    def __init__(
        self,
        prefix: str = _PSEUDO_PREFIX,
        suffix: str = _PSEUDO_SUFFIX,
        anchor: Anchor | None = None,
    ) -> None:
        self._prefix = prefix
        self._suffix = suffix
        self._anchor = anchor
        # ``[A-Z0-9_]+`` covers both suffix styles: a numeric counter and a
        # hex anchor token.  Anchoring on a trailing ``_\d+`` would silently
        # fail to match anchored pseudonyms.
        self._pattern = re.compile(re.escape(prefix) + r"[A-Z0-9_]+" + re.escape(suffix))

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
        """Mint a fresh pseudonym for ``entity_type``.

        With an anchor, the suffix is derived from the value, so the same input
        produces the same pseudonym in every session.  Without one, it is a
        per-session counter that probes past any value already taken: deleting
        a mapping entry makes ``len()`` go backwards, which would otherwise
        re-issue a pseudonym still bound to a different original.
        """
        if self._anchor is not None:
            token = await self._anchor.anchor(original, context)
            return f"{self._prefix}{entity_type}_{self._token(token)}{self._suffix}"

        counter = len(session.mapping) + 1
        pseudo = f"{self._prefix}{entity_type}_{counter}{self._suffix}"
        while pseudo in session.mapping:
            counter += 1
            pseudo = f"{self._prefix}{entity_type}_{counter}{self._suffix}"
        return pseudo

    @staticmethod
    def _token(anchored: str) -> str:
        """Reduce an anchor's output to a short uppercase hex token.

        Anchors return their own format (``PRIVYX_<64 hex>`` for HMAC); only
        the digest tail is kept, so the pseudonym stays readable and matches
        the ``[A-Z0-9_]+`` placeholder grammar.
        """
        digest = anchored.rsplit("_", 1)[-1]
        return digest[:ANCHOR_TOKEN_LENGTH].upper()

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore all pseudonyms in ``text`` using the session mapping."""
        return restore(text, session, self._pattern)