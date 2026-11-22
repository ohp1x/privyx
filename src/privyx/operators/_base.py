"""The operator kind: replace a detection's spans in a text, and restore them."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from privyx.core import Component, Context, Detection, Services, Session, TransformResult
from privyx.token import FormatCodec

if TYPE_CHECKING:
    from privyx.anchors import Anchor

#: Hex characters of a digest kept in an anchored or encrypted token's id
#: (``operator.id_length``).  16 hex chars = 64 bits, far beyond collision
#: range for a single conversation while staying short enough to read in a log.
ID_LENGTH = 16


class Operator(Component, kind="operator"):
    """Replace detected spans with substitutes, and restore them from a session.

    The token codec and the anchor come from :meth:`open`; built directly, an
    operator writes the built-in token syntax, without an anchor.
    """

    #: How a stream of this operator's output is restored: codec tokens,
    #: recognized by syntax, or the exact substituted values (a trie).
    stream_restore: ClassVar[Literal["token", "literal"]] = "token"
    #: Whether the operator can use an anchor; the app reads the per-user
    #: key of ``anchor.secret: auto`` only for one that can.
    uses_anchor: ClassVar[bool] = True
    #: Text written for every value, whatever the value (``redact``'s
    #: ``[REDACTED]``); the DLP check does not look for a masked value inside
    #: it.  Empty when the output depends on the value.
    fixed_text: str = ""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._codec: FormatCodec = FormatCodec.default()
        self._anchor: Anchor | None = None

    async def open(self, services: Services) -> None:
        """Take the app's token codec and its anchor."""
        self._codec = services.codec
        self._anchor = services.anchor

    @abstractmethod
    async def pseudonymize(
        self, text: str, detection: Detection, session: Session, context: Context
    ) -> TransformResult:
        """Replace the spans of ``detection`` in ``text``, keeping each original in ``session``."""

    @abstractmethod
    async def deanonymize(self, text: str, session: Session, context: Context) -> TransformResult:
        """Restore the substitutes in ``text`` that ``session`` holds."""

    def build_resolver(self, mapping: dict[str, str]) -> Callable[[str], str | None]:
        """Map a substitute to its original value, for restore, batch and streamed.

        The default is the session mapping's own lookup, because most operators
        store the plaintext there.  ``encrypt`` stores ciphertext and returns a
        resolver that decrypts.
        """
        return mapping.get
