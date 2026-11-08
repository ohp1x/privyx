"""Partial operator — hide most of a value, keep a little context.

``alice@example.com`` becomes ``a****@example.com`` and ``4111 1111 1111 1234``
becomes ``**** **** **** 1234``: the model can still tell which card or which
mail domain a sentence is about, without the value itself.

What stays visible is not reversible on its own, so the session vault keeps
the original, and a reply is restored by matching the exact masked strings, as
:class:`~privyx.privacy.operator.faker.FakerOperator` does.
"""

from __future__ import annotations

from privyx.privacy.operator.faker import LiteralOperator

#: Characters left visible at the end of a value that is not an email address.
KEEP = 4

#: The fewest ``*`` a masked value holds, so it never reads as Markdown
#: emphasis (``**b``) that a reply could contain by chance.
_MIN_STARS = 4


class PartialOperator(LiteralOperator):
    """Replace each value with a partly masked copy of itself.

    An email address keeps the first character of its local part and its
    domain.  Any other value keeps its punctuation and its last :data:`KEEP`
    letters and digits, never more than a third of them; the rest become
    ``*``.  Two values that would mask the same (two cards ending in ``1234``)
    get one more leading ``*`` each, so every masked value restores to one
    original.
    """

    name = "partial"

    def _fake_for(self, entity_type: str, original: str, salt: int = 0) -> str:
        return mask(original, extra=salt)


def mask(value: str, extra: int = 0) -> str:
    """``value`` partly masked, with ``extra`` more ``*`` in front of the mask."""
    local, at, domain = value.rpartition("@")
    if at and local and domain:
        return f"{local[0]}{'*' * (_MIN_STARS + extra)}@{domain}"
    alnum = [i for i, c in enumerate(value) if c.isalnum()]
    hidden = alnum[: len(alnum) - min(KEEP, len(alnum) // 3)]
    chars = list(value)
    for i in hidden:
        chars[i] = "*"
    pad = max(0, _MIN_STARS - len(hidden)) + extra
    return "*" * pad + "".join(chars)
