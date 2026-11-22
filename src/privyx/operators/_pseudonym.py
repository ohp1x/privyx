"""Pseudonymization operator using reversible tokens.

The operator works with :class:`~privyx.token.model.LogicalToken` values and
delegates their textual form to the token codec, so it never mentions
delimiters or a fixed syntax.  The identifier is a per-session counter by
default, or — with an :class:`~privyx.anchors.Anchor` — a deterministic token
derived from the value itself, so the same input yields the same pseudonym in
every session.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache, partial
from typing import Any

from pydantic import Field

from privyx.core import (
    Context,
    Detection,
    Services,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._base import ID_LENGTH
from privyx.operators._restore import TokenOperator, free_token
from privyx.token.codec import LabelCodec
from privyx.token.model import LogicalToken

#: How many labels to try for a value before its placeholder goes without one.
_LABEL_ATTEMPTS = 8


class PseudonymOperator(TokenOperator):
    """Replace detected spans with deterministic pseudonym tokens.

    Each unique original value maps to a consistent token via the session's
    ``reverse`` mapping.  New values get a fresh identifier: a per-session
    counter, or an anchor-derived token with an anchor, which makes
    pseudonyms deterministic across sessions.  The vault is still the source
    of truth for restoring them — an anchor is one-way — so it changes how
    pseudonyms are *named*, not how they are reversed.  An anchor digest
    keeps ``id_length`` hex characters in the id; a collision makes it
    longer.

    With a :class:`~privyx.token.codec.LabelCodec` (``token.label_format``),
    a placeholder also carries a fake value the model reads, from Faker with
    ``locale`` and ``seed``.  The session still maps the token, never the
    label; and an id then stands for one value whatever the type, so a
    placeholder whose format has no ``{type}`` points to one value.
    """

    name = "pseudonym"

    class Options(TokenOperator.Options):
        #: Hex characters of an anchored token's id; a counter is unaffected.
        id_length: int = Field(default=ID_LENGTH, ge=6, le=64)
        seed: int | None = None  # determinism salt of the labels
        locale: str = ""  # Faker locale of the labels (empty → Faker's default)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        #: ``labels(entity_type, token, salt)``: a label of a placeholder,
        #: another for each ``salt``, or ``None`` for a type without one.
        self._labels: Callable[[str, str, int], str | None] | None = None
        # A request masks the whole conversation again, and a label costs a
        # Faker call or several; the same token and value give the same labels.
        self._choices = lru_cache(maxsize=4096)(self._label_choices)

    async def open(self, services: Services) -> None:
        """Take the codec and anchor; with a label codec, load Faker for the labels.

        Raises:
            ConfigError: If the codec writes labels and Faker is not installed.
        """
        await super().open(services)
        self._labels = None
        if isinstance(self._codec, LabelCodec):
            from privyx.operators._faker import FakeLabels

            self._labels = FakeLabels(locale=self.options.locale or None, seed=self.options.seed)
        self._choices.cache_clear()

    @property
    def value_derived(self) -> bool:
        """With an anchor whose key every process shares."""
        return self._anchor is not None and self._anchor.shared

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)

        # Assign pseudonyms in document order so counters read left to right.
        pseudos: list[str] = []
        for span in detection.spans:
            original = span.text
            known = session.pseudonym_for(original)
            pseudo = known or await self._issue(span.entity_type, original, session, context)
            session.put(pseudo, original)
            if self._labels and not known:
                self._noted(session, pseudo, original)
            pseudos.append(pseudo)
        # Labeled once every value of the text is known: a label must hold none.
        transforms = [
            Transformation(
                span.start,
                span.end,
                span.text,
                self._labeled(session, pseudo, span.text) if self._labels else pseudo,
                span.entity_type,
            )
            for span, pseudo in zip(detection.spans, pseudos, strict=True)
        ]

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
                self.options.id_length,
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
        """The ids of the session's tokens, whatever their type."""
        return self._index(session).ids

    def _index(self, session: Session) -> _Issued:
        """The session's ids and values, kept on it as tokens are issued.

        Indexing every token for each new value would cost values squared per
        request.
        """
        size = len(session.mapping)
        if session.issued is None or session.issued[0] != size:
            index = _Issued()
            for token, value in session.mapping.items():
                index.add(self._codec.parse(token), value)
            session.issued = (size, index)
        index = session.issued[1]
        assert isinstance(index, _Issued)
        return index

    def _noted(self, session: Session, pseudo: str, original: str) -> None:
        """Add ``pseudo``, just issued for ``original``, to the session's index."""
        cached = session.issued
        if cached is not None and cached[0] == len(session.mapping) - 1:
            cached[1].add(self._codec.parse(pseudo), original)
            session.issued = (len(session.mapping), cached[1])

    def _labeled(self, session: Session, pseudo: str, original: str) -> str:
        """``pseudo`` written with a label, or as it is when no label fits.

        The first of :meth:`_label_choices` that holds no other value of the session
        (``James Wilson`` while ``James`` is masked too), which would send that
        value and fail ``proxy.dlp_check``.
        """
        index = self._index(session)
        for label, written in self._choices(pseudo, original):
            if not index.within(label, original):
                return written
        return pseudo

    def _label_choices(self, pseudo: str, original: str) -> tuple[tuple[str, str], ...]:
        """``(label, written)`` for each label ``pseudo`` can carry, the best first.

        A label comes from the token, which the provider sees anyway, never
        from the value, so it tells nothing about the value. A label that holds
        the value is never used.  One that shares no word with it is preferred:
        a fake name must not carry the real first name.  Words such as ``com``
        or ``https``, which most emails and URLs share, are why that is a
        preference.
        """
        assert self._labels is not None and isinstance(self._codec, LabelCodec)
        token = self._codec.parse(pseudo)
        if token is None:
            return ()
        words = _words(original)
        best: list[tuple[str, str]] = []
        rest: list[tuple[str, str]] = []
        for salt in range(_LABEL_ATTEMPTS):
            label = self._labels(token.type, pseudo, salt)
            if label is None:
                return ()  # a type without fake values
            written = self._codec.label(token, label)
            if written is None or original.casefold() in label.casefold():
                continue
            (rest if words & _words(label) else best).append((label, written))
        return (*best, *rest)

    @staticmethod
    def _digest(anchored: str) -> str:
        """Reduce an anchor's output to its uppercase digest.

        Anchors return their own format (``PRIVYX_<64 hex>`` for HMAC); only the
        digest tail is kept, and the token uses its first ``id_length``
        characters.
        """
        return anchored.rsplit("_", 1)[-1].upper()


def _words(text: str) -> set[str]:
    """The words of ``text`` of three characters or more, case-folded."""
    return set(re.findall(r"\w{3,}", text.casefold()))


class _Issued:
    """A session's token ids, and its values by their first word, for labels."""

    __slots__ = ("by_word", "ids")

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.by_word: dict[str, list[str]] = {}

    def add(self, token: LogicalToken | None, value: str) -> None:
        if token is not None:
            self.ids.add(token.identifier)
        first = _FIRST.search(value.casefold())
        if first:
            self.by_word.setdefault(first.group(), []).append(value)

    def within(self, label: str, own: str) -> bool:
        """Whether ``label`` holds a value of the session other than ``own``.

        A value in the label begins with one of its words, so only the values
        filed under those words are looked for.
        """
        folded = label.casefold()
        return any(
            value != own and value.casefold() in folded
            for word in set(_FIRST.findall(folded))
            for value in self.by_word.get(word, ())
        )


_FIRST = re.compile(r"\w+")
