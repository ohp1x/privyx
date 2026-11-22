"""The restore routine the reversible operators share, and the minting of free token ids."""

from __future__ import annotations

from collections.abc import Callable

from privyx.core import (
    Context,
    OperatorError,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._base import Operator
from privyx.token import FormatCodec
from privyx.token.codec import TokenIds
from privyx.token.model import LogicalToken


class TokenOperator(Operator):
    """An operator whose substitutes are codec tokens, restored through the session."""

    def _encode(self, entity_type: str, identifier: str) -> str:
        """The token of ``entity_type`` with the id ``identifier``."""
        return self._codec.encode(
            LogicalToken(namespace=self._codec.namespace, type=entity_type, identifier=identifier)
        )

    async def deanonymize(self, text: str, session: Session, context: Context) -> TransformResult:
        """Restore the tokens this session issued, and leave any other as it is."""
        return restore(text, session, self._codec, resolve=self.build_resolver(session.mapping))


def free_token(
    session: Session,
    stored: str,
    digest: str,
    length: int,
    encode: Callable[[str], str],
    taken: Callable[[str], bool] | None = None,
) -> str:
    """Return the token for ``stored`` whose id is ``digest[:length]``, or longer.

    When another value of this session already holds that token, the id grows
    from the same digest one character at a time until it is free, so a
    collision never overwrites a mapping and restores the wrong value.  The same
    value walks the same prefixes, so it gets the same token within a session.
    ``taken`` says whether an id is taken anyway, by a token of another type.

    Raises:
        OperatorError: If every prefix of ``digest`` is taken by another value.
    """
    for end in range(min(length, len(digest)), len(digest) + 1):
        token = encode(digest[:end])
        if session.mapping.get(token, stored) == stored and not (taken and taken(digest[:end])):
            return token
    raise OperatorError(
        f"no free token id: every prefix of a {len(digest)}-character digest is taken"
    )


def restore(
    text: str,
    session: Session,
    codec: FormatCodec,
    resolve: Callable[[str], str | None] | None = None,
) -> TransformResult:
    """Replace every token in ``text`` that ``session`` knows about.

    Shared by the reversible operators: the *codec* decides what a token looks
    like, while the replacement and the "leave unknown tokens alone" rule live
    here rather than being reimplemented per operator.  No operator hard-codes
    token syntax.

    Unknown tokens are passed through untouched: a stream may legitimately
    contain text that fits the token syntax but was never issued by us, and
    dropping or mangling it would corrupt the response.  A token the model wrote
    without its edge literals (``PRIVYX_EMAIL_1``) or one of them, in another
    case, or as its id alone when the id is long enough, is restored as well,
    under the same rule: only if this session issued it.

    Args:
        text: Text containing tokens.
        session: Source of truth for token text → original.
        codec: Locates tokens in ``text``.
        resolve: Maps a token's text to its original value, or ``None`` when the
            token is unknown.  Defaults to the session mapping's own lookup;
            ``encrypt`` supplies a decrypting resolver because its mapping stores
            ciphertext rather than plaintext.
    """
    resolve = resolve if resolve is not None else session.get
    transforms: list[Transformation] = []

    # A token may be written without its edge literals, or as its id alone.
    ids = _token_ids(session, codec)
    for start, end, token in sorted(codec.restorable(text, ids)):
        original = resolve(token)
        if original is None and (issued := ids.cased(token)):
            original = resolve(issued)  # written in another case
        if original is None:
            continue
        transforms.append(Transformation(start, end, text[start:end], original))

    return TransformResult(text=apply_replacements(text, transforms), transformations=transforms)


def _token_ids(session: Session, codec: FormatCodec) -> TokenIds:
    """``session``'s tokens by id, indexed again only once it has issued more.

    A response is restored one string at a time, and indexing every token for
    each of them would cost strings × tokens.
    """
    size = len(session.mapping)
    if session.token_ids is None or session.token_ids[0] != size:
        session.token_ids = (size, codec.ids(session.mapping))
    ids: TokenIds = session.token_ids[1]
    return ids
