"""Operators whose substitutes are plain text, restored by exact match."""

from __future__ import annotations

from privyx.core import (
    Context,
    Detection,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._base import Operator
from privyx.streaming.trie import PseudonymTrie

#: How many times to re-roll a fake before falling back to a numeric suffix, when
#: a generated value collides with a *different* original already in the session
#: (or equals the original, which would leak it).  Collisions are rare for the
#: large-space providers (email, phone) and handled deterministically for the
#: small-space ones (names) by salting the seed.
_MAX_ATTEMPTS = 8


class LiteralOperator(Operator):
    """Replace detected spans with plain-text substitutes, restored by exact match.

    The shared half of :class:`~privyx.operators.FakerOperator` and the other operators whose
    output is ordinary text rather than a codec token.  Each unique original
    maps to one substitute via the session's ``reverse`` mapping, exactly like
    :class:`~privyx.operators.PseudonymOperator`, so the same value reads the same way throughout
    a conversation.  A subclass only says what the substitute is
    (:meth:`_fake_for`).
    """

    #: This operator's output is plain text, so a stream is reversed by matching
    #: the exact substituted values (a trie), not the codec token syntax.
    stream_restore = "literal"
    uses_anchor = False

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)

        # Assign fakes in document order (so reuse within the text is stable).
        transforms: list[Transformation] = []
        for span in detection.spans:
            original = span.text
            fake = session.pseudonym_for(original) or self._issue(
                span.entity_type, original, session
            )
            session.put(fake, original)
            transforms.append(
                Transformation(span.start, span.end, original, fake, span.entity_type)
            )

        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore fakes by matching the exact substituted strings.

        Runs the same trie hold-back scan the streaming
        :class:`~privyx.streaming.deanonymizer.StreamingDeanonymizer` performs,
        fed as a single chunk — so ``stream == batch`` holds by construction, not
        by two implementations happening to agree.  A value the session never
        issued is left alone.
        """
        return _restore_literal(text, session.mapping)

    def _issue(self, entity_type: str, original: str, session: Session) -> str:
        """Mint a fresh fake, re-rolling on a collision with a different original.

        A fake equal to the original would leak it; a fake already bound to a
        *different* original would make restoration ambiguous.  Both are avoided
        deterministically by salting the seed, with a numeric-suffix fallback for
        the pathological case where every re-roll still collides.
        """
        fake = ""
        for salt in range(_MAX_ATTEMPTS):
            fake = self._fake_for(entity_type, original, salt)
            if not fake or fake == original:
                continue
            existing = session.mapping.get(fake)
            if existing is None or existing == original:
                return fake
        return f"{fake}-{len(session.mapping) + 1}"

    def _fake_for(self, entity_type: str, original: str, salt: int = 0) -> str:
        """The substitute for ``original``; a new ``salt`` must give a new one."""
        raise NotImplementedError


def _restore_literal(text: str, mapping: dict[str, str]) -> TransformResult:
    """Replace substituted values in ``text`` with their originals.

    Scans ``text`` for pseudonyms in ``mapping``, matching the longest known
    value at each position.  Because the hold-back scan in
    :class:`~privyx.streaming.deanonymizer.StreamingDeanonymizer` drains any
    held tail the same way at the end of a stream, batch and streaming results
    agree.  A trie is reused (rather than the codec) because a fake value has no
    token syntax.
    """
    trie = PseudonymTrie()
    trie.update(mapping)
    out: list[str] = []
    transforms: list[Transformation] = []
    i, n = 0, len(text)
    while i < n:
        match = trie.match_at(text, i)
        if match is not None:
            matched, original = match
            out.append(original)
            transforms.append(Transformation(i, i + len(matched), matched, original))
            i += len(matched)
            continue
        out.append(text[i])
        i += 1

    return TransformResult(text="".join(out), transformations=transforms)
