"""Tests for the JSON walk and the key probe in :mod:`privyx.privacy.transform.json`."""

from __future__ import annotations

import json

from privyx.core.result import Span
from privyx.privacy.transform.json import map_values, value_spans, with_key


def test_value_spans_land_on_the_characters_json_escaped() -> None:
    """Every character, escaped or not, maps back to itself; a span ending
    inside an escape takes the character, one starting inside it does not."""
    key, value = "k", 'a"b\\c\nd\x01e✓'
    start = len(key) + 3
    for i, char in enumerate(value):
        at = start + len(json.dumps(value[:i], ensure_ascii=False)) - 2
        size = len(json.dumps(char, ensure_ascii=False)) - 2
        for low, high in ((at, at + size), (at, at + 1)):
            (span,) = value_spans(key, value, [Span(low, high, "X", "")])
            assert (span.start, span.end, span.text) == (i, i + 1, char)
        if size > 1:  # from inside the escape: only what follows it
            assert value_spans(key, value, [Span(at + 1, at + size, "X", "")]) == []
            (span,) = value_spans(key, value, [Span(at + size - 1, at + size + 1, "X", "")])
            assert (span.start, span.end) == (i + 1, i + 2)
    # Cut to the value: the key and the quotes are not part of it.
    (span,) = value_spans(key, value, [Span(0, len(with_key(key, value)), "X", "")])
    assert (span.start, span.end) == (0, len(value))


async def test_map_values_gives_each_leaf_its_key() -> None:
    """The key a string leaf is under, also through lists; numbers only with
    ``under``, and alone."""
    seen: list[tuple[str | None, str]] = []

    async def fn(text: str) -> str:
        seen.append((None, text))
        return "masked" if text == "4" else text.upper()

    async def under(key: str, text: str) -> str:
        seen.append((key, text))
        return "masked" if text == "b" else text

    payload = {"a": "b", "c": [["d"], 4, 5], "e": True, "f": None, "g": 1.5}
    out = await map_values([payload, "top", 7], fn, under)

    assert out == [
        {"a": "masked", "c": [["d"], "masked", 5], "e": True, "f": None, "g": 1.5},
        "TOP",
        7,
    ]
    assert seen == [
        ("a", "b"),
        ("c", "d"),
        (None, "4"),
        (None, "5"),
        (None, "1.5"),
        (None, "top"),
        (None, "7"),
    ]

    # Without `under` (restoring), only strings, and with no key.
    seen.clear()
    assert await map_values(payload, fn) == {**payload, "a": "B", "c": [["D"], 4, 5]}
    assert seen == [(None, "b"), (None, "d")]


async def test_map_values_paths_select_by_prefix() -> None:
    async def fn(text: str) -> str:
        return "x"

    payload = {"user": {"email": "a"}, "items": [{"note": "b", "id": "c"}], "other": "d"}
    out = await map_values(payload, fn, paths=["$.user", "$.items[*].note"])

    assert out == {"user": {"email": "x"}, "items": [{"note": "x", "id": "c"}], "other": "d"}
