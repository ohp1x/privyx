"""Find values hidden from the patterns: split into strings or encoded.

An agent that cannot send a secret as is may send ``'s' + 'k' + '-' + …`` or
its Base64.  :func:`hidden` joins string concatenations and decodes Base64,
hex, and URL encoding, runs the detector on the result, and reports the whole
expression or blob that holds a finding.  URL-encoded text is the exception:
each character maps back, so only the finding itself is marked.

Decoding is bounded: blobs of up to 64 KiB, two levels deep (Base64 inside
Base64), and 1,000,000 characters of candidates per scan, charged before each
is decoded.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from itertools import chain

from privyx.core.result import Detection

MAX_DEPTH = 2
MAX_DECODED = 1_000_000

_STRING = r"""(?:"(?:\\.|[^"\\\r\n])*+"|'(?:\\.|[^'\\\r\n])*+'|chr\(\d{1,7}\))"""
#: String literals or ``chr(…)`` calls joined by ``+``, ``.`` (PHP), or ``..`` (Lua).
_CONCAT = re.compile(rf"{_STRING}(?:[ \t]*(?:\+|\.\.?)[ \t]*{_STRING})++")
_PART = re.compile(r""""((?:\\.|[^"\\\r\n])*+)"|'((?:\\.|[^'\\\r\n])*+)'|chr\((\d{1,7})\)""")

_BLOB = re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{16,65536}+={0,2}(?![\w+/=-])")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_TOKEN = re.compile(r"""[^\s"'<>]+""")
_ESCAPE = re.compile(r"%([0-7][0-9a-fA-F])")


def _joined(expression: str) -> str:
    parts = []
    for double, single, code in _PART.findall(expression):
        if code:
            if int(code) > 0x10FFFF:
                return ""
            parts.append(chr(int(code)))
        else:
            parts.append(double or single)
    return "".join(parts)


def _text(raw: bytes) -> str | None:
    """``raw`` as text, if it is printable UTF-8."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if all(char.isprintable() or char in "\t\r\n" for char in text):
        return text
    return None


def _decoded(blob: str) -> str | None:
    """Hex or (URL-safe) Base64 ``blob`` as text."""
    if _HEX.fullmatch(blob):
        return _text(bytes.fromhex(blob))
    body = blob.rstrip("=").replace("-", "+").replace("_", "/")
    if len(body) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
    except binascii.Error:
        return None
    return _text(raw)


def hidden(
    text: str, detect: Callable[[str], Detection], depth: int = 0, budget: list[int] | None = None
) -> Detection:
    """Spans of ``text`` that hide what ``detect`` finds once joined or decoded.

    ``budget`` holds the characters left to decode, shared with the nested
    scans.  Each candidate is charged its length before it is decoded, and the
    scan stops once a candidate no longer fits.
    """
    if budget is None:
        budget = [MAX_DECODED]
    found = Detection()
    candidates = chain(
        ((m, _joined) for m in _CONCAT.finditer(text)),
        ((m, _decoded) for m in _BLOB.finditer(text)),
    )
    for match, decoder in candidates:
        if not _charge(budget, len(match[0])):
            return found
        decoded = decoder(match[0])
        if not decoded:
            continue
        spans = detect(decoded).spans
        if not spans and depth + 1 < MAX_DEPTH:
            spans = hidden(decoded, detect, depth + 1, budget).spans
        if spans:
            entity = min(spans, key=lambda s: s.start).entity_type
            found.add(match.start(), match.end(), entity, match[0])

    if "%" in text:
        for token in _TOKEN.finditer(text):
            if _ESCAPE.search(token[0]):
                if not _charge(budget, len(token[0])):
                    return found
                found.spans += _unescaped(token, detect).spans
    return found


def _charge(budget: list[int], cost: int) -> bool:
    """Take ``cost`` from ``budget`` if it fits."""
    if cost > budget[0]:
        budget[0] = 0
        return False
    budget[0] -= cost
    return True


def _unescaped(token: re.Match[str], detect: Callable[[str], Detection]) -> Detection:
    """What ``detect`` finds in ``token`` with its ASCII ``%XX`` escapes decoded.

    Only spans that cross an escape are kept, mapped back to the escaped text;
    the others the detector already found in ``token`` as written.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pos, raw = token.start(), token[0]
    i = 0
    while i < len(raw):
        escape = _ESCAPE.match(raw, i)
        width = 3 if escape else 1
        chars.append(chr(int(escape[1], 16)) if escape else raw[i])
        starts.append(pos + i)
        ends.append(pos + i + width)
        i += width
    found = Detection()
    if len(chars) == len(raw):
        return found
    for span in detect("".join(chars)).spans:
        start, end = starts[span.start], ends[span.end - 1]
        if end - start != span.end - span.start:
            found.add(start, end, span.entity_type, token.string[start:end])
    return found
