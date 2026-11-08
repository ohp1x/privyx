"""Faker-based operator — realistic fake values instead of tokens.

Unlike :class:`~privyx.privacy.operator.pseudonym.PseudonymOperator`, which
substitutes a syntactically distinctive token (``<PRIVYX_EMAIL_1>``), this
operator replaces each detected span with a *realistic* fake of the same kind: a
fake email for an ``EMAIL``, a fake name for a ``PERSON``.  Keeping the shape of
the data lets a model reason over it naturally.

That realism has a cost the token approach does not pay — a fake value is
ordinary text with no delimiter — and it shows up in two places, both documented
on the methods below:

* **Literal restore.**  Reversal matches the *exact substituted strings* (via the
  session mapping), not a token grammar.  Batch and streaming share that
  matching, so ``stream == batch`` still holds; the streaming side selects a trie
  recognizer because this operator sets :attr:`stream_restore` to ``"literal"``.
* **Possible false positives.**  A fake value that also occurs naturally in a
  response can be restored by coincidence — a risk the token operators cannot
  have.  Prefer ``pseudonym`` when collision-free reversal matters more than
  realism.

``faker`` is an optional dependency; selecting this operator without it installed
fails at startup with a :class:`~privyx.core.errors.ConfigError` rather than on
the first request (mirroring how a missing vault extra fails in the builder).
"""

from __future__ import annotations

import hashlib

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator
from privyx.privacy.transform.text import apply_replacements
from privyx.streaming.trie import PseudonymTrie

#: How many times to re-roll a fake before falling back to a numeric suffix, when
#: a generated value collides with a *different* original already in the session
#: (or equals the original, which would leak it).  Collisions are rare for the
#: large-space providers (email, phone) and handled deterministically for the
#: small-space ones (names) by salting the seed.
_MAX_ATTEMPTS = 8


#: Entity type (upper-cased) → the Faker method that fakes it.  Covers the
#: built-in detector's types plus the common Presidio/YAML ones; anything else
#: falls back to a neutral two-word phrase.  Dispatch is by method *name* so the
#: (untyped) Faker instance is only ever reached through ``getattr``.
_PROVIDER_METHODS: dict[str, str] = {
    "EMAIL": "ascii_email",
    "PHONE": "phone_number",
    "PHONE_NUMBER": "phone_number",
    "SSN": "ssn",
    "CREDIT_CARD": "credit_card_number",
    "IP_ADDRESS": "ipv4",
    "ZIPCODE": "postcode",
    "PERSON": "name",
    "NAME": "name",
    "LOCATION": "city",
    "ADDRESS": "street_address",
    "ORGANIZATION": "company",
    "ORG": "company",
    "COMPANY": "company",
    "URL": "url",
    "DATE": "date",
    "DATE_TIME": "iso8601",
}


class FakerOperator(BaseOperator):
    """Replace detected spans with realistic fake values of the same kind.

    Each unique original maps to a consistent fake via the session's ``reverse``
    mapping, exactly like :class:`PseudonymOperator` — so the same value reads the
    same way throughout a conversation.  The fake is derived deterministically
    from the value (and the optional ``seed``), so a given original also fakes
    identically across sessions and runs, which keeps the operator testable.

    Args:
        locale: Faker locale (e.g. ``en_US``, ``id_ID``); Faker's default when
            omitted.
        seed: Optional salt mixed into the per-value seed.  Two deployments with
            different seeds fake the same value differently; the same seed
            reproduces the same fakes.

    Raises:
        ConfigError: If the ``faker`` package is not installed.
    """

    name = "faker"
    #: This operator's output is plain text, so a stream is reversed by matching
    #: the exact substituted values (a trie), not the codec token syntax.
    stream_restore = "literal"

    def __init__(self, locale: str | None = None, seed: int | None = None) -> None:
        try:
            from faker import Faker
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ConfigError(
                "operator type 'faker' requires the 'faker' extra; install with "
                "`pip install privyx[faker]` (or `uv sync --all-extras`)"
            ) from exc
        self._faker = Faker(locale) if locale else Faker()
        self._seed = seed

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
            transforms.append(Transformation(span.start, span.end, original, fake))

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
        """Deterministically fake ``original`` for ``entity_type``.

        The Faker instance is seeded from a hash of the value so the same input
        yields the same fake every time, independent of any global RNG state.
        """
        digest = hashlib.sha256(
            f"{self._seed}:{entity_type}:{original}:{salt}".encode()
        ).hexdigest()
        self._faker.seed_instance(int(digest[:16], 16))
        method = _PROVIDER_METHODS.get(entity_type.upper())
        if method is None:
            return f"{self._faker.word()}-{self._faker.word()}"
        return str(getattr(self._faker, method)())


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
