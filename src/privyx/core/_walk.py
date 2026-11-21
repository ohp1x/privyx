"""One walk over the string leaves of a JSON value, shared by masking, restoring, and streams.

The walk is a generator: it yields each leaf, receives its replacement, and
returns the rebuilt value, so the async mask and restore and the synchronous
stream restore share it.  An API format (a dialect) only says which keys hold
tool-argument documents and which are opaque.

A key whose value is structural or opaque is skipped with its whole subtree:
ids, enums, tool and function names, signatures, binary payloads, and URLs.
A detector match there would corrupt the request, and none holds prose.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator, Mapping
from typing import Any

from privyx.core._json import leaf_text, put_back

#: Keys skipped with their subtree in every format.  Any key ending in ``_id``
#: or ``_url`` or starting with ``encrypted_`` is opaque too.
OPAQUE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "type",
        "role",
        "name",
        "signature",
        "data",
        "file_data",
        "url",
        "media_type",
        "cache_control",
        "status",
    }
)

#: Keys whose object value is a tool-argument document, unless a format names others.
DOCUMENT_KEYS: frozenset[str] = frozenset({"input", "arguments"})

#: A generator that yields each string leaf, receives its replacement, and
#: returns the rebuilt value.
Walk = Generator[str, str, Any]


class Keyed(str):
    """A leaf of a tool-argument document, with the key it is under.

    Yielded only when the walk is ``keyed`` (masking): a value may only say
    what it is with its key, as ``{"password": "hunter2"}`` does.
    """

    key: str

    def __new__(cls, text: str, key: str) -> Keyed:
        leaf = super().__new__(cls, text)
        leaf.key = key
        return leaf


def opaque(key: str, parent: Mapping[str, Any], extra: frozenset[str] = frozenset()) -> bool:
    """Whether ``key`` (in the object ``parent``) is structural, not content.

    ``extra`` holds the opaque keys a format adds to :data:`OPAQUE_KEYS`.
    """
    if key == "data" and parent.get("type") == "text":
        return False  # a plain-text document source: the document itself
    if key == "result" and parent.get("type") == "image_generation_call":
        return True  # base64 image, echoed back in a Responses input
    if key in extra:
        return True
    return key in OPAQUE_KEYS or key.endswith(("_id", "_url")) or key.startswith("encrypted_")


def walk(
    value: Any,
    key: str | None,
    document: bool = False,
    *,
    documents: frozenset[str] = DOCUMENT_KEYS,
    hidden: frozenset[str] = frozenset(),
    keyed: bool = False,
) -> Walk:
    """Yield every content string leaf of ``value`` and return the rebuilt value.

    ``key`` is the key ``value`` sits under.  A tool-argument *document* — an
    object under one of ``documents`` (Anthropic ``tool_use`` ``input``,
    ``arguments``), or the JSON a string ``arguments`` holds — is user data
    with no wire structure, so inside it no key is opaque (a ``name``
    argument is PII).  A string ``arguments`` is walked parsed and
    re-serialized, never as one text blob (a match could straddle a JSON
    escape); unparseable arguments are walked as plain text, never passed
    through raw.  ``hidden`` holds the opaque keys the format adds.

    With ``keyed`` (masking), a string in a document under a key, also from
    inside a list, is yielded as a :class:`Keyed`.  A number in a document is
    yielded as text, alone (with its key, ``{"token_limit": 4000}`` would
    read as a secret), and goes back as it was unless masked.
    """
    if isinstance(value, str):
        if not value:
            return value
        if key == "arguments":
            try:
                parsed = json.loads(value)
            except ValueError:
                return (yield value)
            rebuilt = yield from walk(
                parsed, None, True, documents=documents, hidden=hidden, keyed=keyed
            )
            return value if rebuilt == parsed else json.dumps(rebuilt, ensure_ascii=False)
    if keyed and document and key is not None and (text := leaf_text(value)) is not None:
        leaf = Keyed(text, key) if isinstance(value, str) else text
        return put_back(value, text, (yield leaf))
    if isinstance(value, str):
        return (yield value)
    if isinstance(value, list):
        items = []
        for item in value:
            items.append(
                (
                    yield from walk(
                        item, key, document, documents=documents, hidden=hidden, keyed=keyed
                    )
                )
            )
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not document and opaque(k, value, hidden):
                out[k] = v
            else:
                nested = document or (k in documents and isinstance(v, dict))
                out[k] = yield from walk(
                    v, k, nested, documents=documents, hidden=hidden, keyed=keyed
                )
        return out
    return value


def walk_sync(
    value: Any,
    fn: Callable[[str], str],
    key: str | None = None,
    *,
    documents: frozenset[str] = DOCUMENT_KEYS,
    hidden: frozenset[str] = frozenset(),
) -> Any:
    """Rebuild ``value`` with ``fn`` applied to each content string leaf."""
    walker = walk(value, key, documents=documents, hidden=hidden)
    try:
        text = next(walker)
        while True:
            text = walker.send(fn(text))
    except StopIteration as done:
        return done.value


async def walk_async(
    value: Any,
    fn: Callable[[str], Awaitable[str]],
    key: str | None = None,
    *,
    documents: frozenset[str] = DOCUMENT_KEYS,
    hidden: frozenset[str] = frozenset(),
) -> Any:
    """Async twin of :func:`walk_sync`, for the engine's ``transform`` and ``restore``."""
    return await drive(walk(value, key, documents=documents, hidden=hidden), fn)


async def drive(walker: Walk, fn: Callable[[str], Awaitable[str]]) -> Any:
    """Feed each leaf ``walker`` yields through ``fn``; return the rebuilt value."""
    try:
        text = next(walker)
        while True:
            text = walker.send(await fn(text))
    except StopIteration as done:
        return done.value
