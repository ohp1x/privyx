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
import re
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


_JSON_STRING_BOUNDARY = re.compile(r'\\[\s\S]?|"')
_INCOMPLETE_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{0,3})?\Z")
_JSON_SYNTAX = re.compile(r"[\s{}\[\],:0-9.eE+truefalsn-]*")


class JsonStrings:
    """Rewrite JSON strings and preserve the spelling of the surrounding syntax."""

    def __init__(
        self,
        *,
        restore_outside: bool = False,
        plain_feed: Callable[[str], str] | None = None,
        plain_flush: Callable[[], str] | None = None,
    ) -> None:
        self._parts: list[str] = []
        self._escaped = False
        self._restore_outside = restore_outside
        self._plain_feed = plain_feed
        self._plain_flush = plain_flush
        self._prose: bool | None = None
        self._invalid_syntax = False

    def feed(self, text: str) -> Generator[str, str, str]:
        if self._restore_outside:
            if self._prose is None:
                leading = text.lstrip()
                if not leading:
                    return text
                self._prose = leading[0] not in '{["-0123456789'
            if self._prose:
                return (yield from self._plain(text))

        out: list[str] = []
        at = 0
        while at < len(text):
            if not self._parts:
                start = text.find('"', at)
                if start == -1:
                    out.append((yield from self._outside(text[at:])))
                    break
                out.append((yield from self._outside(text[at:start])))
                out.append(self._finish_outside())
                self._parts.append('"')
                at = start + 1
            cursor = at + 1 if self._escaped else at
            self._escaped = False
            for match in _JSON_STRING_BOUNDARY.finditer(text, cursor):
                if match[0] == '"':
                    end = match.end()
                    self._parts.append(text[at:end])
                    out.append((yield from self._rewrite_string(self._take_string(), closed=True)))
                    at = end
                    break
                if len(match[0]) == 1:
                    self._escaped = True
            else:
                self._parts.append(text[at:])
                break
        return "".join(out)

    def flush(self) -> Generator[str, str, str]:
        prefix = self._finish_outside()
        tail = self._take_string()
        return prefix + ((yield from self._rewrite_string(tail, closed=False)) if tail else "")

    def _plain(self, text: str) -> Generator[str, str, str]:
        if self._plain_feed is not None:
            return self._plain_feed(text)
        return (yield text)

    def _outside(self, text: str) -> Generator[str, str, str]:
        if not text or not self._restore_outside:
            return text
        if self._invalid_syntax:
            return (yield from self._plain(text))
        # Leave JSON punctuation, keywords and numbers outside strings untouched.
        prefix = _JSON_SYNTAX.match(text)
        assert prefix is not None
        end = prefix.end()
        self._invalid_syntax = end < len(text)
        return text if end == len(text) else text[:end] + (yield from self._plain(text[end:]))

    def _finish_outside(self) -> str:
        self._invalid_syntax = False
        return self._plain_flush() if self._plain_flush is not None else ""

    def _take_string(self) -> str:
        tail = "".join(self._parts)
        self._parts.clear()
        self._escaped = False
        return tail

    @staticmethod
    def _rewrite_string(literal: str, *, closed: bool) -> Generator[str, str, str]:
        suffix = ""
        try:
            if closed:
                decoded = json.loads(literal)
            else:
                # Decode the portion before the cut and retain any unfinished escape.
                try:
                    decoded = json.loads(literal + '"')
                except ValueError:
                    partial = _INCOMPLETE_ESCAPE.search(literal)
                    if partial is None:
                        raise
                    suffix = partial[0]
                    decoded = json.loads(literal[: partial.start()] + '"')
        except ValueError:
            return (yield literal)
        changed = yield decoded
        if changed == decoded:
            return literal
        encoded = json.dumps(changed)
        return encoded if closed else encoded[:-1] + suffix


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
    json_text: bool = False,
    assistant_json: bool = False,
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

    With ``json_text``, text blocks carry embedded JSON. With ``assistant_json``,
    this also applies to assistant history in a Messages request.

    With ``keyed`` (masking), a string in a document under a key, also from
    inside a list, is yielded as a :class:`Keyed`.  A number in a document is
    yielded as text, alone (with its key, ``{"token_limit": 4000}`` would
    read as a secret), and goes back as it was unless masked.
    """
    if isinstance(value, str):
        if not value:
            return value
        if json_text and not document and key in ("text", "content"):
            # A client can echo a partial JSON document after an interrupted reply.
            if keyed and not value.lstrip().startswith(('"', "{", "[")) and "\\" not in value:
                return (yield value)
            strings = JsonStrings(restore_outside=not keyed)
            rewritten = (yield from strings.feed(value)) + (yield from strings.flush())
            # Keep the ordinary text scan for detections based on JSON syntax.
            return (yield rewritten) if keyed else rewritten
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
                        item,
                        key,
                        document,
                        documents=documents,
                        hidden=hidden,
                        keyed=keyed,
                        json_text=json_text,
                        assistant_json=assistant_json,
                    )
                )
            )
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        assistant = assistant_json and key == "messages" and value.get("role") == "assistant"
        for k, v in value.items():
            if not document and opaque(k, value, hidden):
                out[k] = v
            else:
                nested = document or (k in documents and isinstance(v, dict))
                out[k] = yield from walk(
                    v,
                    k,
                    nested,
                    documents=documents,
                    hidden=hidden,
                    keyed=keyed,
                    json_text=json_text or (assistant and k == "content"),
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
    json_text: bool = False,
) -> Any:
    """Rebuild ``value`` with ``fn`` applied to each content string leaf."""
    return drive_sync(walk(value, key, documents=documents, hidden=hidden, json_text=json_text), fn)


def drive_sync(walker: Walk, fn: Callable[[str], str]) -> Any:
    """Feed each leaf a walk yields through a synchronous callback."""
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
    json_text: bool = False,
) -> Any:
    """Async twin of :func:`walk_sync`, for the engine's ``transform`` and ``restore``."""
    return await drive(
        walk(value, key, documents=documents, hidden=hidden, json_text=json_text), fn
    )


async def drive(walker: Walk, fn: Callable[[str], Awaitable[str]]) -> Any:
    """Feed each leaf ``walker`` yields through ``fn``; return the rebuilt value."""
    try:
        text = next(walker)
        while True:
            text = walker.send(await fn(text))
    except StopIteration as done:
        return done.value
