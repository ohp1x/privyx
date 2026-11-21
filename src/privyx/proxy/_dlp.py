"""The final leak check of a masked request, and the copies it masks first.

The check behind ``proxy.dlp_check``: a value masked in one place of a request
but sent as is in another (a field that is not walked, a URL, a header) is
found here, and the request is refused.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from typing import Any

from privyx.core import SECRET_TYPES
from privyx.token.codec import FormatCodec


def find_leaks(
    value: Any,
    masked: set[str],
    *,
    underscore: bool = False,
    codec: FormatCodec | None = None,
    fixed: str = "",
    written: int | None = None,
    tools: frozenset[str] = frozenset(),
    glued: Collection[str] = (),
) -> set[str]:
    """The ``masked`` values that are still in ``value``'s strings.

    The check behind ``proxy.dlp_check``: a value masked in one place of a
    request but sent as is in another (a field that is not walked, a URL, a
    header) is found here, and the request is refused. The caller reinstates
    remembered upstream thinking only after this check. Keys count as strings,
    and a value that starts or ends with a letter or digit must stand as a
    whole word there (``Bob`` is not found in ``Bobby``), unless it is
    ``glued``: then also inside a word (``hunter2-x9`` in ``-phunter2-x9``).
    With ``underscore``, as the detector has it, ``_`` separates words (``Bob``
    is in ``DB_BOB``).
    The tokens of ``codec`` are not searched, nor does a match count that lies
    inside the ``fixed`` text an operator writes for every value, where that
    text stands as a word: a value may be part of the token that replaced it
    (``PERSON`` in ``<PRIVYX_PERSON_1>``, ``TED`` in ``[TED-HIDDEN]``).  When
    more copies of that text hold a masked value than the ``written`` times
    the operator wrote it, they all count: a raw ``Bob`` cannot be told from
    the ``Bob`` of ``redact.text: Bob``.  Text an operator derives from a
    value, such as a ``partial`` mask or a placeholder's label, still is.
    Neither are the ``tools`` names where a ``name`` or ``tool_name`` holds
    one: they are sent as declared, so a term in ``mcp__acme__search`` would
    refuse every request.
    """
    sent = "\0".join(_strings(value, tools))
    # A plain substring search first: most values are not there at all.
    words = sorted((w for w in masked if w in sent), key=len, reverse=True)
    if not words:
        return set()
    if not any("\0" in word for word in words):
        # Only the strings that hold one are searched further: \0 ends each.
        held: set[tuple[int, int]] = set()
        for word in words:
            at = sent.find(word)
            while at >= 0:
                end = sent.find("\0", at)
                end = len(sent) if end < 0 else end
                held.add((sent.rfind("\0", 0, at) + 1, end))
                at = sent.find(word, end)
        sent = "\0".join(sent[start:end] for start, end in sorted(held))
    # longest first
    pattern = re.compile("|".join(_copy(word, underscore, glued) for word in words))
    if pattern.search(sent) is None:
        return set()  # only inside other words: no token to look past
    if codec is not None:
        parts, at = [], 0
        for token in codec.finditer(sent):
            parts.append(sent[at : token.start])
            if token.label is not None:  # the label is text like any other
                parts.append(sent[slice(*token.label)])
            at = token.end
        sent = "\0".join([*parts, sent[at:]])
    # A match is the operator's own text only where it lies inside that text:
    # cut out, the text would split a value around it (`john x smith`).
    spans = [m.span() for m in re.finditer(_whole(fixed, underscore), sent)] if fixed else []
    leaked: set[str] = set()
    inside: dict[tuple[int, int], str] = {}  # the operator's text a match lies in
    for m in pattern.finditer(sent):
        span = next((s for s in spans if s[0] <= m.start() and m.end() <= s[1]), None)
        if span is None:
            leaked.add(m.group())
        else:
            inside[span] = m.group()
    if written is not None and len(inside) > written:
        leaked.update(inside.values())  # more of that text than the operator wrote
    return leaked


def _mask_copies(
    text: str,
    copies: dict[str, str],
    *,
    underscore: bool,
    codec: FormatCodec,
    fixed: str,
    glued: Collection[str],
) -> tuple[str, int]:
    """``text`` with each copy of a key of ``copies`` replaced by its value, and how many.

    A copy is what :func:`find_leaks` would find: a whole word unless
    ``glued``, not in a token of ``codec`` nor inside the ``fixed`` text.
    """
    words = sorted((w for w in copies if w in text), key=len, reverse=True)
    if not words:
        return text, 0
    tokens = [(token.start, token.end) for token in codec.finditer(text)]
    fixed_spans = [m.span() for m in re.finditer(_whole(fixed, underscore), text)] if fixed else []
    parts: list[str] = []
    at = 0
    for m in re.finditer("|".join(_copy(word, underscore, glued) for word in words), text):
        start, end = m.span()
        if any(s < end and start < e for s, e in tokens) or any(
            s <= start and end <= e for s, e in fixed_spans
        ):
            continue
        parts += (text[at:start], copies[m.group()])
        at = end
    return "".join([*parts, text[at:]]), len(parts) // 2


#: JSON-schema keywords in ``tools`` that hold values, not labels.
_SCHEMA_VALUES = frozenset({"default", "const", "enum", "examples", "example"})


def _searched(request: dict[str, Any], wrapped: frozenset[str]) -> dict[str, Any]:
    """``request`` as the leak check sees it: of its ``tools``, the descriptions and values.

    The rest of ``tools`` is sent as declared (see :func:`_descriptions`): a
    term in a property key or ``title`` would refuse every request.  A value
    such as a ``default`` is data, so it is still searched.
    """
    out = dict(request)
    if "tools" in out:
        out["tools"] = list(_tool_texts(out["tools"]))
    for key in wrapped & out.keys():
        if isinstance(out[key], dict):
            out[key] = _searched(out[key], wrapped)
    return out


def _tool_texts(value: Any, key: str | None = None) -> Iterator[str]:
    """The masked descriptions of a ``tools`` value, and the values its schemas hold."""
    if key in _SCHEMA_VALUES:
        yield from _strings(value)
    elif isinstance(value, str):
        if key == "description":
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _tool_texts(item, key)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _tool_texts(v, k)


def _glued(value: str, entity_type: str | None) -> bool:
    """Whether a copy of ``value`` counts also glued to other text, as in ``-phunter2-x9``.

    For a credential of at least 8 characters with a digit or symbol in it:
    ``postgres`` of ``PGPASSWORD=postgres`` is no copy in ``postgresql``.
    """
    return (
        entity_type in SECRET_TYPES
        and len(value) >= 8
        and any(not (char.isalpha() or char.isspace()) for char in value)
    )


def _copy(word: str, underscore: bool, glued: Collection[str]) -> str:
    return re.escape(word) if word in glued else _whole(word, underscore)


def _whole(word: str, underscore: bool = False) -> str:
    word_char = r"[^\W_]" if underscore else r"\w"
    left = rf"(?<!{word_char})" if word[0].isalnum() else ""
    right = rf"(?!{word_char})" if word[-1].isalnum() else ""
    return f"{left}{re.escape(word)}{right}"


def _strings(value: Any, tools: frozenset[str] = frozenset()) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)  # a masked tool-argument number, sent as one elsewhere
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            if key in ("name", "tool_name") and isinstance(item, str) and item in tools:
                continue
            yield from _strings(item, tools)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, tools)


def _declared_names(payload: Mapping[str, Any], wrapped: frozenset[str]) -> frozenset[str]:
    """The tool names a request declares, a ``wrapped`` request's included."""
    names = _tool_names(payload.get("tools"))
    for key in wrapped & payload.keys():
        if isinstance(payload[key], dict):
            names |= _tool_names(payload[key].get("tools"))
    return names


def _tool_names(tools: Any) -> frozenset[str]:
    """The tool names ``tools`` declares: ``name``, ``function.name``, or Gemini's declarations."""
    names: set[str] = set()
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        declared = tool.get("functionDeclarations") or tool.get("function_declarations") or []
        for item in [tool, tool.get("function"), *declared]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
    return frozenset(names)
