"""Find values written with look-alike or invisible characters.

``ｊｏｈｎ`` (fullwidth letters) or ``jo\\u200bhn`` (a zero-width space inside)
reads as ``john`` but matches no pattern or term.  Text copied from a web page,
a PDF, or an input method carries such characters without anyone meaning to.
:func:`normalized` folds each character to its NFKC compatibility form and
drops format characters (Unicode category ``Cf``, the zero-width ones among
them), remembering where each folded character came from, so what is found
there is marked on the text as written.

Folding is per character, so composed and decomposed accents stay as they are:
``jóhn`` is not ``john``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache

from privyx.core.result import Detection

_NON_ASCII = re.compile(r"[^\x00-\x7f]+")


@lru_cache(maxsize=4096)
def _fold(char: str) -> str:
    if unicodedata.category(char) == "Cf":
        return ""
    return unicodedata.normalize("NFKC", char)


def normalized(text: str, detect: Callable[[str], Detection]) -> Detection:
    """Spans of ``text`` that hold what ``detect`` finds once ``text`` is folded.

    Every span is kept, also one whose own text did not change: what folded
    may be the context around it, such as ``ＰＡＳＳＷＯＲＤ=`` before an ASCII
    secret or a fullwidth context word.  Empty when nothing in ``text`` folds.
    """
    found = Detection()
    if text.isascii() or not any(
        _fold(char) != char for run in _NON_ASCII.finditer(text) for char in set(run[0])
    ):
        return found
    parts: list[str] = []
    source: list[int] = []  # where each folded character starts in `text`
    end: list[int] = []  # and ends
    pos = 0
    for run in _NON_ASCII.finditer(text):
        parts.append(text[pos : run.start()])
        source.extend(range(pos, run.start()))
        end.extend(range(pos + 1, run.start() + 1))
        for i, char in enumerate(run[0], run.start()):
            folded = _fold(char)
            parts.append(folded)
            source.extend([i] * len(folded))
            end.extend([i + 1] * len(folded))
        pos = run.end()
    parts.append(text[pos:])
    source.extend(range(pos, len(text)))
    end.extend(range(pos + 1, len(text) + 1))
    folded_text = "".join(parts)
    for span in detect(folded_text).spans:
        start, stop = source[span.start], end[span.end - 1]
        found.add(start, stop, span.entity_type, text[start:stop])
    return found
