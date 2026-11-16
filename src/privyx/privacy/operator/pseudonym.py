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

import re
from collections.abc import Callable
from functools import lru_cache, partial

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.anchor.base import Anchor
from privyx.privacy.operator.base import BaseOperator, free_token, restore
from privyx.privacy.transform.text import apply_replacements
from privyx.token.codec import FormatCodec, LabelCodec, TokenCodec
from privyx.token.model import LogicalToken

#: Default hex characters of an anchor digest to keep (``token.id_length``).
#: 16 hex chars = 64 bits of the HMAC, far beyond collision range for a single
#: conversation while staying short enough to read in a log.
ANCHOR_TOKEN_LENGTH = 16

#: How many labels to try for a value before its placeholder goes without one.
_LABEL_ATTEMPTS = 8


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
        id_length: Hex characters of the anchor digest kept in the token id;
            a collision makes it longer.
        labels: With a :class:`~privyx.token.codec.LabelCodec`, gives the
            label of a placeholder: ``labels(entity_type, original, salt)``,
            another for each ``salt``, or ``None`` for a type without one.
            The session still maps the token, never the label; and an id then
            stands for one value whatever the type, so a placeholder whose
            format has no ``{type}`` points to one value.
    """

    name = "pseudonym"

    def __init__(
        self,
        codec: TokenCodec | None = None,
        anchor: Anchor | None = None,
        id_length: int = ANCHOR_TOKEN_LENGTH,
        labels: Callable[[str, str, int], str | None] | None = None,
    ) -> None:
        self._codec = codec or FormatCodec.default()
        self._anchor = anchor
        self._id_length = id_length
        self._labels = labels if isinstance(self._codec, LabelCodec) else None
        # A request masks the whole conversation again, and a label costs a
        # Faker call or several; the same token and value give the same label.
        self._written = lru_cache(maxsize=4096)(self._labeled)

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)

        # Assign pseudonyms in document order so counters read left to right.
        transforms: list[Transformation] = []
        for span in detection.spans:
            original = span.text
            known = session.pseudonym_for(original)
            pseudo = known or await self._issue(span.entity_type, original, session, context)
            session.put(pseudo, original)
            if self._labels and not known:
                self._noted(session, pseudo)
            written = self._written(pseudo, original) if self._labels else pseudo
            transforms.append(Transformation(span.start, span.end, original, written))

        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )

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
        # With labels, an id another type holds is taken too.
        used = self._ids(session) if self._labels else set()
        if self._anchor is not None:
            anchored = await self._anchor.anchor(original, context)
            return free_token(
                session,
                original,
                self._digest(anchored),
                self._id_length,
                partial(self._encode, entity_type),
                used.__contains__ if used else None,
            )

        counter = len(session.mapping) + 1
        pseudo = self._encode(entity_type, str(counter))
        while pseudo in session.mapping or str(counter) in used:
            counter += 1
            pseudo = self._encode(entity_type, str(counter))
        return pseudo

    def _ids(self, session: Session) -> set[str]:
        """The ids of the session's tokens, whatever their type.

        Kept on the session as tokens are issued: indexing every token for each
        new value would cost values squared per request.
        """
        size = len(session.mapping)
        if session.issued_ids is None or session.issued_ids[0] != size:
            tokens = (self._codec.parse(token) for token in session.mapping)
            session.issued_ids = (size, {token.identifier for token in tokens if token})
        return session.issued_ids[1]

    def _noted(self, session: Session, pseudo: str) -> None:
        """Add the id of ``pseudo``, just issued, to the session's ids."""
        token = self._codec.parse(pseudo)
        cached = session.issued_ids
        if token is not None and cached is not None and cached[0] == len(session.mapping) - 1:
            cached[1].add(token.identifier)
            session.issued_ids = (len(session.mapping), cached[1])

    def _labeled(self, pseudo: str, original: str) -> str:
        """``pseudo`` written with a label, or as it is when no label can be written.

        A label that is the value itself, in any case, is never used.  One that
        shares no word with the value is preferred: a fake name must not carry
        the real first name.  Words such as ``com`` or ``https``, which most
        emails and URLs share, are why that is a preference.
        """
        assert self._labels is not None and isinstance(self._codec, LabelCodec)
        token = self._codec.parse(pseudo)
        if token is None:
            return pseudo
        words = _words(original)
        usable = None
        for salt in range(_LABEL_ATTEMPTS):
            label = self._labels(token.type, original, salt)
            if label is None:
                return pseudo  # a type without fake values
            if label.casefold() == original.casefold():
                continue
            written = self._codec.label(token, label)
            if written is not None and not words & _words(label):
                return written
            usable = usable or written
        return usable or pseudo

    def _encode(self, entity_type: str, identifier: str) -> str:
        return self._codec.encode(
            LogicalToken(
                namespace=self._codec.namespace,
                type=entity_type,
                identifier=identifier,
            )
        )

    @staticmethod
    def _digest(anchored: str) -> str:
        """Reduce an anchor's output to its uppercase digest.

        Anchors return their own format (``PRIVYX_<64 hex>`` for HMAC); only the
        digest tail is kept, and the token uses its first ``id_length``
        characters.
        """
        return anchored.rsplit("_", 1)[-1].upper()

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore all tokens in ``text`` using the session mapping."""
        return restore(text, session, self._codec)


def _words(text: str) -> set[str]:
    """The words of ``text`` of three characters or more, case-folded."""
    return set(re.findall(r"\w{3,}", text.casefold()))
