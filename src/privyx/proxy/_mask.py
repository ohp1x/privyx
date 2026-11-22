"""Mask a request and restore its reply, as a dialect says where their text is.

A request is walked leaf by leaf, and only what can hold prose is masked:

- the top-level keys the dialect names as content
  (:attr:`~privyx.dialects.Dialect.content_keys`); everything else there is
  config (``model``, ``response_format``, …) and is forwarded as-is.  Inside
  them every string leaf is masked except the opaque keys.  It fails closed:
  an unknown field is masked, never forwarded raw.  ``tools`` is config too,
  except its ``description`` prose (:func:`_descriptions`): an MCP server
  writes those, and they reach the model on every turn.  What a client reports
  about its own machine (an agent type's ``metadata_keys``) is walked whole.
- a reply is restored in *every* string leaf but the opaque ones: placeholders
  only exist because Privyx minted them, so restoring anywhere is safe.

Two things keep a multi-turn conversation byte-stable, which prompt caching and
Anthropic's thinking signatures both need: the request is walked in cache-prefix
order (the dialect's ``prefix_keys``, then the rest, client metadata last), so
a value first seen later cannot renumber the system prompt (a
``defer_loading`` tool is not part of that prefix: the provider renders it
where a ``tool_reference`` loads it, so it is walked right after the message
that does, see :func:`_loading`); and the dialect puts back the text the
upstream signed into an echoed block, after the leak check
(:meth:`~privyx.dialects.Dialect.reinstate`).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from privyx.core import Keyed, LeakError, Session, Walk, drive, walk_async
from privyx.core import walk as walk_value
from privyx.dialects import CONTENT_KEYS, PREFIX_KEYS, Dialect
from privyx.engine import PrivacyEngine
from privyx.events import CREDENTIAL_HEADERS
from privyx.proxy._dlp import (
    _declared_names,
    _glued,
    _mask_copies,
    _searched,
    _strings,
    find_leaks,
)

#: Query parameter names used for authentication (never masked).
AUTH_QUERY_PARAMS: frozenset[str] = frozenset({"key", "api_key", "api-key", "access_token"})


class _Plain(Dialect):
    """The keys of most chat formats, for a request masked without a dialect."""

    content_keys = CONTENT_KEYS
    prefix_keys = PREFIX_KEYS


_PLAIN = _Plain()


async def transform_request(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
    *,
    session: Session | None = None,
    headers: dict[str, str] | None = None,
    params: list[tuple[str, Any]] | None = None,
    dialect: Dialect | None = None,
    metadata_keys: frozenset[str] | None = None,
    masked_headers: frozenset[str] | None = None,
    masked: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with its content leaves pseudonymized.

    Only :data:`CONTENT_KEYS` are walked; inside them every string leaf is
    pseudonymized except the opaque keys (see :func:`_opaque`), including the
    assistant turns a client echoes back — a restored value that is not
    pseudonymized again would reach the upstream in the clear.

    ``metadata_keys``, in which a client describes the machine it runs on (the
    agent types' :attr:`~privyx.agents.Agent.metadata_keys`; by default, the
    built-in types', as with ``masked_headers``), are walked whole:
    no key is opaque in there, and a file list sits under ``status``, a remote
    under ``url``.  They are walked last: some come with only one request of a
    conversation, and numbered earlier their values would shift every token
    after them on that turn alone.  ``headers``, the ones about to be sent
    upstream, belong to the same request: the values of ``masked_headers`` are
    pseudonymized **in place**, after the body and in the same session, so a
    path gets one token in both.

    ``session``, an :meth:`~privyx.engine.PrivacyEngine.ephemeral_session`,
    is used as is: no lock, no vault read or write.  Otherwise ``session_id`` is
    held through :meth:`~privyx.engine.PrivacyEngine.session_scope`.

    The caller's ``payload`` is never mutated (the walk rebuilds every container
    it descends into; config values are shared, not copied).

    With ``engine.dlp_check``, a value masked in one text but still in another
    as a word (``x`` of ``DB_PASSWORD=x``, masked only by its name) gets the
    replacement it got first there too, in every text the walk masks.  A
    masked value still in the request after that raises
    :class:`~privyx.core.LeakError` (:func:`find_leaks`).

    ``dialect`` says which keys hold the conversation; without one, those of
    most chat formats.

    ``masked``, when given, is awaited with the masked request, its copies
    masked too, before the final leak check: it may change the request and
    ``headers`` in place (the ``request.masked`` event), and the strings it
    adds are checked as well.  Only then does the dialect put back the text
    the upstream signed (:meth:`~privyx.dialects.Dialect.reinstate`).
    """
    dialect = dialect or _PLAIN
    prefix = dialect.prefix_keys
    metadata_keys, masked_headers = agent_metadata(metadata_keys, masked_headers)

    def rank(key: str) -> int:
        return len(prefix) + 1 if key in metadata_keys else _prefix_rank(key, prefix)

    def sent(request: dict[str, Any]) -> list[Any]:
        """What the leak check reads of a request about to be sent.

        Not the credential headers: a key pasted into a prompt is sent there too.
        """
        return [
            _searched(request, dialect.wrapped_keys),
            [v for k, v in (headers or {}).items() if k.lower() not in CREDENTIAL_HEADERS],
            [[k, v] for k, v in params or [] if k.lower() not in AUTH_QUERY_PARAMS],
        ]

    rebuilt: dict[str, Any] = {}
    replaced: dict[str, str] = {}  # what this request masked, for engine.dlp_check, and with what
    glued: set[str] = set()  # ... and which of it counts glued to other text too
    written = 0  # ... and how many values it replaced
    texts: list[str] = []  # ... and each text as masked, in walk order
    leak_check: dict[str, Any] = {
        "underscore": engine.underscore_separates_words,
        "codec": engine.codec,
        "fixed": engine.operator.fixed_text,
        "glued": glued,
    }
    scope: AbstractAsyncContextManager[Session] = (
        engine.session_scope(session_id) if session is None else nullcontext(session)
    )
    async with scope as held:

        async def transform(text: str) -> str:
            nonlocal written
            # `{"password": "hunter2"}`: a tool argument may be a secret by its key.
            key = text.key if isinstance(text, Keyed) else None
            result = await engine.transform(str(text), session=held, persist=False, key=key)
            if engine.dlp_check:
                for t in result.transformations:
                    replaced.setdefault(t.original, t.replacement)
                    if _glued(t.original, t.entity_type):
                        glued.add(t.original)
                written += len(result.transformations)
                texts.append(result.text)
            return result.text

        async def walk(transform: Callable[[str], Awaitable[str]]) -> None:
            async def metadata(text: str) -> str:
                return await transform(_unescaped(text))

            content_keys = dialect.content_keys
            deferred: dict[str, Any] = {}  # deferred tools by name, walked where they load
            loaded: dict[str, Any] = {}  # ... and once walked
            for key in sorted(payload, key=rank):  # stable: the rest keep their order
                value = payload[key]
                if key == "tools":
                    rebuilt[key] = await drive(_tools(value, deferred), transform)
                elif key == "messages" and deferred and isinstance(value, list):
                    rebuilt[key] = await drive(
                        _loading(value, dialect, deferred, loaded), transform
                    )
                elif key in content_keys:
                    rebuilt[key] = await drive(_content(value, key, dialect), transform)
                elif key in metadata_keys:
                    rebuilt[key] = await drive(walk_value(value, key, True), metadata)
                else:
                    rebuilt[key] = value
            for name, tool in deferred.items():  # not loaded yet: the provider renders it nowhere
                if name not in loaded:
                    loaded[name] = await drive(_descriptions(tool), transform)
            if loaded:
                rebuilt["tools"] = [
                    loaded[t["name"]] if _deferred(t) else t for t in rebuilt["tools"]
                ]
            if headers:
                for name in masked_headers & headers.keys():
                    headers[name] = _header_value(await metadata(headers[name]))
            if params is not None:
                for idx, (name, val) in enumerate(list(params)):
                    if name.lower() not in AUTH_QUERY_PARAMS:
                        params[idx] = (name, await metadata(val))

        def leaks(values: set[str]) -> set[str]:
            if not values:
                return set()
            tools = _declared_names(payload, dialect.wrapped_keys)
            return find_leaks(sent(rebuilt), values, written=written, tools=tools, **leak_check)

        await walk(transform)
        if leaked := leaks(set(replaced)):
            # The same walk again, with each text as masked: no detection, only
            # the copies replaced.  Only these can be left, the rest was checked.
            copies = {value: replaced[value] for value in leaked}
            masked_texts = iter(texts)

            async def with_copies(_: str) -> str:
                nonlocal written
                text, count = _mask_copies(next(masked_texts), copies, **leak_check)
                written += count
                return text

            await walk(with_copies)
            if leaked := leaks(set(copies)):
                raise LeakError(f"{len(leaked)} masked value(s) still in the request")
    result = {key: rebuilt[key] for key in payload}
    if masked is not None:
        declared = _declared_names(payload, dialect.wrapped_keys)
        check = engine.dlp_check and bool(replaced)
        checked = set(_strings(sent(result), declared)) if check else set()
        await masked(result)
        if check:
            # Only what the listeners wrote: the rest was checked above.
            added = [text for text in _strings(sent(result), declared) if text not in checked]
            if leaked := find_leaks(added, set(replaced), **leak_check):
                raise LeakError(f"{len(leaked)} masked value(s) in what a listener added")
    # Only text remembered from an upstream response may bypass DLP: it
    # must match its signature. All client fields have been checked above.
    dialect.reinstate(result)
    return result


def agent_metadata(
    keys: frozenset[str] | None, headers: frozenset[str] | None
) -> tuple[frozenset[str], frozenset[str]]:
    """``keys`` and ``headers``, with the built-in agent types' for one left out."""
    if keys is None or headers is None:
        from privyx.agents import builtin_metadata

        builtin_keys, builtin_headers = builtin_metadata()
        keys = builtin_keys if keys is None else keys
        headers = builtin_headers if headers is None else headers
    return keys, headers


def _unescaped(text: str) -> str:
    """``text`` with its ``\\uXXXX`` escapes decoded, when it is a JSON document.

    Codex writes a workspace under ``/home/josé`` as ``/home/jos\\u00e9``, which
    no detector would match.  Such a document is then pseudonymized as text, not
    leaf by leaf: the paths in it are keys.
    """
    if "\\u" not in text:
        return text
    try:
        decoded = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
        decoded.encode()  # a lone surrogate could not be sent
    except ValueError:
        return text
    return decoded


def _header_value(text: str) -> str:
    """``text`` as one line of printable ASCII, all a header value may hold.

    Any other character becomes its JSON escape (``é`` → ``\\u00e9``), which the
    JSON these headers hold reads back unchanged.
    """
    return "".join(c if " " <= c <= "~" else json.dumps(c)[1:-1] for c in text)


async def restore_response(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
    *,
    session: Session | None = None,
    dialect: Dialect | None = None,
) -> dict[str, Any]:
    """Return a copy of a batch ``payload`` with pseudonyms restored.

    Every string leaf is restored whatever the route's schema (OpenAI-compatible
    gateways often answer ``/v1/messages`` with a ``chat.completion`` body); only
    opaque keys are skipped, so a token-shaped id is left alone.  ``dialect``
    adds its own document and opaque keys (Gemini's ``functionCall.args``),
    and first notes what the reply holds that a later request echoes.
    ``session``, when the caller holds it, skips the vault read.  When the
    session is unknown the payload is returned untouched.
    """
    dialect = dialect or _PLAIN
    dialect.remember(payload)
    if session is None:
        session = await engine.vault.get(session_id)
    if session is None:
        return payload

    async def restore(text: str) -> str:
        return (await engine.restore(text, session_id, session=session)).text

    result: dict[str, Any] = await walk_async(
        payload, restore, documents=dialect.document_keys, hidden=dialect.opaque_keys
    )
    return result


def _prefix_rank(key: str, prefix: tuple[str, ...]) -> int:
    """Where ``key`` goes in the walk: ``prefix`` first, in its order."""
    return prefix.index(key) if key in prefix else len(prefix)


def _content(value: Any, key: str, dialect: Dialect) -> Walk:
    """Walk the content ``value`` of a request's top-level ``key``.

    A request the dialect wraps in another (Gemini's ``countTokens``) is walked
    as a request: its content keys and ``tools`` descriptions, nothing else.
    """
    if key in dialect.wrapped_keys and isinstance(value, dict):
        return (yield from _wrapped_request(value, dialect))
    return (
        yield from walk_value(
            value, key, documents=dialect.document_keys, hidden=dialect.opaque_keys, keyed=True
        )
    )


def _wrapped_request(value: dict[str, Any], dialect: Dialect) -> Walk:
    out: dict[str, Any] = {}
    for k in sorted(value, key=lambda k: _prefix_rank(k, dialect.prefix_keys)):
        v = value[k]
        if k == "tools":
            out[k] = yield from _descriptions(v)
        elif k in dialect.content_keys:
            out[k] = yield from _content(v, k, dialect)
        else:
            out[k] = v
    return {k: out[k] for k in value}


def _deferred(tool: Any) -> bool:
    """Whether ``tool`` is loaded on demand (``defer_loading``) instead of up front."""
    return (
        isinstance(tool, dict)
        and tool.get("defer_loading") is True
        and isinstance(tool.get("name"), str)
    )


def _tools(value: Any, deferred: dict[str, Any]) -> Walk:
    """Walk the descriptions in ``tools``, leaving its deferred tools to ``deferred``.

    A client adds a tool it loaded with tool search to ``tools`` on a later turn
    (Claude Code's ToolSearch).  Numbered here, a value it names would renumber
    the system prompt and every message on that turn: a prompt-cache miss.
    """
    if not isinstance(value, list):
        return (yield from _descriptions(value))
    out = []
    for tool in value:
        if _deferred(tool):
            deferred[tool["name"]] = tool
            out.append(tool)  # replaced once walked
        else:
            out.append((yield from _descriptions(tool)))
    return out


def _loading(
    messages: list[Any], dialect: Dialect, deferred: dict[str, Any], loaded: dict[str, Any]
) -> Walk:
    """Walk ``messages``, and each ``deferred`` tool right after the message that loads it.

    That is where the provider renders it.  Every turn walks the same messages
    in the same order, so the tool keeps its tokens when later turns add values.
    """
    out = []
    for message in messages:
        out.append((yield from _content(message, "messages", dialect)))
        for name in _references(message):
            if name in deferred and name not in loaded:
                loaded[name] = yield from _descriptions(deferred[name])
    return out


def _references(value: Any) -> Iterator[str]:
    """Names of the tools the ``tool_reference`` blocks in ``value`` load."""
    if isinstance(value, list):
        for item in value:
            yield from _references(item)
    elif isinstance(value, dict):
        if value.get("type") == "tool_reference" and isinstance(value.get("tool_name"), str):
            yield value["tool_name"]
        for item in value.values():
            yield from _references(item)


def _descriptions(value: Any, key: str | None = None) -> Walk:
    """Yield only the ``description`` strings of a ``tools`` value.

    Tool and parameter descriptions are prose (an MCP server's may name a
    customer or an org); everything else there is wire structure the provider
    validates — ``name``, ``enum``, ``pattern``, ``default``, ``required`` — so it
    is kept byte-identical.  A JSON-schema property *named* ``description`` holds
    an object, not a string, and is descended into like any other.
    """
    if isinstance(value, str):
        return (yield value) if key == "description" and value else value
    if isinstance(value, list):
        items = []
        for item in value:
            items.append((yield from _descriptions(item, key)))
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[k] = yield from _descriptions(v, k)
        return out
    return value
