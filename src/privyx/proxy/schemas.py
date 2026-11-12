"""Wire-schema routing and structural transforms.

A *wire schema* is the JSON shape a provider speaks on a given path.  The
transparent proxy needs to know, per request, three things:

1. which path maps to which schema (:func:`detect_schema`);
2. which text leaves of a request body to pseudonymize before forwarding
   (:func:`transform_request`);
3. which text leaves of a batch response to restore (:func:`restore_response`).

Both directions share one leaf walk (:func:`walk_sync` / :func:`walk_async`)
instead of a walker per field, so a field a provider adds later is covered by
default rather than leaking:

- **restore** walks *every* string leaf of a response.  Placeholders only exist
  because Privyx minted them, so restoring anywhere is safe, and one walk serves
  OpenAI Chat, OpenAI Responses, and Anthropic bodies alike;
- **transform** walks only the top-level keys that carry conversation content
  (:data:`CONTENT_KEYS`) — everything else there is config (``model``,
  ``response_format``, …) and is forwarded as-is — and inside them pseudonymizes
  every string leaf except opaque/structural keys (:func:`_opaque`).  It fails
  closed: an unknown field is pseudonymized, never forwarded raw.  ``tools`` is
  config too, except its ``description`` prose (:func:`_descriptions`): an MCP
  server writes those, and they reach the model on every turn.  What a client
  reports about its own machine (:data:`METADATA_KEYS`) is walked whole.

Two things keep a multi-turn conversation byte-stable, which prompt caching and
Anthropic's thinking signatures both need: the request is walked in cache-prefix
order (``tools``, then ``system`` / ``instructions``, then the rest, client
metadata last), so a value first seen later cannot renumber the system prompt;
and an echoed ``thinking`` block gets back the exact text the upstream signed
(:func:`remember_thinking`) instead of a re-pseudonymization of its restored form.

Streaming responses are handled by
:class:`~privyx.proxy.stream_router.StreamRouter`, which reuses the same walk
for every event it does not restore delta by delta.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Generator, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from functools import lru_cache
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import LeakError
from privyx.core.session import Session
from privyx.privacy.transform.json import leaf_text, put_back
from privyx.token.codec import TokenCodec
from privyx.token.model import LogicalToken

#: The schemas whose request/response bodies this module knows how to walk.
KNOWN_SCHEMAS: frozenset[str] = frozenset(
    {"openai", "anthropic", "responses", "embeddings", "gemini", "ollama"}
)

#: Top-level request keys that carry conversation content.  ``messages`` and
#: ``system`` (Chat, Anthropic); ``input``, ``instructions``, and ``prompt``
#: (template variables) for Responses; ``prediction`` (Chat predicted output);
#: ``prompt`` and ``suffix`` (legacy completions); ``input`` (embeddings).
CONTENT_KEYS: frozenset[str] = frozenset(
    {"messages", "system", "input", "instructions", "prompt", "suffix", "prediction"}
)

#: Top-level keys of a Gemini request that carry the conversation, in both of
#: the casings Gemini accepts.  ``generateContentRequest`` is the request a
#: ``countTokens`` call wraps; it is walked as a request of its own.
#: ``content`` and ``title`` are an embedding request's text, and ``requests``
#: the embedding requests of a batch, walked whole.
GEMINI_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "contents",
        "systemInstruction",
        "system_instruction",
        "generateContentRequest",
        "generate_content_request",
        "requests",
        "content",
        "title",
    }
)

_GEMINI_WRAPPED = frozenset({"generateContentRequest", "generate_content_request"})

#: Keys whose object value is a tool-argument document (see :func:`_walk`), per
#: schema.  Gemini's ``functionCall.args`` and ``functionResponse.response``.
_DOCUMENT_KEYS: dict[str | None, frozenset[str]] = {"gemini": frozenset({"args", "response"})}
_DEFAULT_DOCUMENT_KEYS = frozenset({"input", "arguments"})

#: Opaque keys a schema adds to :data:`OPAQUE_KEYS`.  Gemini: the media type and
#: URI of a file part, and the signature of the model's hidden reasoning.
#: Ollama: base64 images, and the tool a tool message answers.
_SCHEMA_OPAQUE_KEYS: dict[str | None, frozenset[str]] = {
    "gemini": frozenset(
        {
            "mimeType",
            "mime_type",
            "fileUri",
            "file_uri",
            "thoughtSignature",
            "thought_signature",
        }
    ),
    "ollama": frozenset({"images", "tool_name"}),
}

#: Keys whose value is structural or opaque — skipped with their whole subtree.
#: Ids, enums, tool/function names, signatures, binary payloads, and URLs: a
#: detector match there would corrupt the request, and none holds prose.  Any
#: key ending in ``_id`` or ``_url`` or starting with ``encrypted_`` is opaque too.
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

#: Top-level request keys in which a client describes the machine it runs on,
#: not the conversation: Claude Code's ``safeguards`` (working directory, home,
#: user name, git branch and remotes) and Codex's ``client_metadata`` (workspace
#: paths and remote URLs).  No key is opaque in there: it is not wire structure
#: this module knows, and a file list sits under ``status``, a remote under ``url``.
METADATA_KEYS: frozenset[str] = frozenset({"safeguards", "client_metadata"})

#: Request headers that carry the same: Codex repeats its turn metadata in one.
MASKED_HEADERS: frozenset[str] = frozenset({"x-codex-turn-metadata"})

#: Request headers that carry the upstream credential: ``proxy.dlp_check`` does
#: not look in them, as a key the user pasted into a prompt is also sent there.
AUTH_HEADERS: frozenset[str] = frozenset(
    {"authorization", "x-api-key", "api-key", "x-goog-api-key", "proxy-authorization"}
)

#: Query parameter names used for authentication (never masked).
AUTH_QUERY_PARAMS: frozenset[str] = frozenset({"key", "api_key", "api-key", "access_token"})

#: Top-level keys walked first, in the order the provider caches the prompt.
_PREFIX_KEYS: tuple[str, ...] = (
    "tools",
    "system",
    "systemInstruction",
    "system_instruction",
    "instructions",
)

#: Thinking text exactly as the upstream sent it, by signature.  The client gets
#: it restored and echoes it back, but re-pseudonymizing that is not always the
#: exact inverse (a value the model wrote itself is new to the detector), and the
#: provider rejects a thinking block whose text no longer matches its signature.
# ponytail: process-local FIFO — lost on restart, not shared between workers;
# move it into the vault if multi-instance deployments hit signature errors.
_THINKING: dict[str, str] = {}
_THINKING_CHARS: int = 0
_THINKING_MAX = 4096
#: Characters held in all, signatures included: bounded by count alone, 4096
#: long thinking blocks could hold hundreds of MB.
_THINKING_MAX_CHARS = 16_000_000

# A generator that yields each string leaf, receives its replacement, and
# returns the rebuilt value.  Written once as a generator so the async
# transform/restore and the synchronous stream restore share one walk.
_Walk = Generator[str, str, Any]


class _Keyed(str):
    """A leaf of a tool-argument document, with the key it is under.

    Yielded only to :func:`transform_request`, which masks it seen with the key.
    """

    key: str

    def __new__(cls, text: str, key: str) -> _Keyed:
        leaf = super().__new__(cls, text)
        leaf.key = key
        return leaf


def detect_schema(path: str, routes: dict[str, str]) -> str | None:
    """Return the wire schema configured for ``path``, or ``None``.

    ``path`` is matched with a single leading slash, so both ``v1/messages``
    (as FastAPI hands it to a ``{path:path}`` route) and ``/v1/messages`` resolve
    the same way.

    A path that repeats ``/v1`` (``/v1/v1/messages``, what an Anthropic client
    calls when its base URL ends in a ``/v1`` of its own) gets the schema of the
    route it means: the upstream answers 404 either way, and the body must not
    reach it unmasked.

    A ``*`` in a route matches any characters but ``/``, as the model name in
    ``/v1beta/models/*:generateContent``.  A route without one wins over a
    route with one; between those, the first listed wins.
    """
    normalized = "/" + path.lstrip("/")
    schema = routes.get(normalized)
    if schema is None and normalized.startswith("/v1/v1/"):
        schema = routes.get(normalized[3:])
    if schema is None:
        for pattern, route in _route_patterns(tuple(r for r in routes if "*" in r)):
            if pattern.fullmatch(normalized):
                return routes[route]
    return schema


@lru_cache(maxsize=16)
def _route_patterns(routes: tuple[str, ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Each route with a ``*`` compiled to a regex, in order."""
    return tuple((re.compile("[^/]+".join(map(re.escape, r.split("*")))), r) for r in routes)


def has_token_ids(payload: dict[str, Any]) -> bool:
    """Whether ``input`` or ``prompt`` holds token ids instead of text.

    Embeddings and legacy completions accept ``[1, 2]`` or ``[[1, 2]]``.  Only
    text is scanned, and the upstream decodes the ids back to the original, so
    such a request must be refused rather than forwarded.
    """
    for key in ("input", "prompt"):
        value = payload.get(key)
        for item in value if isinstance(value, list) else []:
            if isinstance(item, int) or (
                isinstance(item, list) and any(isinstance(i, int) for i in item)
            ):
                return True
    return False


async def transform_request(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
    *,
    session: Session | None = None,
    headers: dict[str, str] | None = None,
    params: list[tuple[str, Any]] | None = None,
    schema: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with its content leaves pseudonymized.

    Only :data:`CONTENT_KEYS` are walked; inside them every string leaf is
    pseudonymized except the opaque keys (see :func:`_opaque`), including the
    assistant turns a client echoes back — a restored value that is not
    pseudonymized again would reach the upstream in the clear.

    :data:`METADATA_KEYS` are walked whole, and last: some come with only one
    request of a conversation, and numbered earlier their values would shift
    every token after them on that turn alone.  ``headers``, the ones about to
    be sent upstream, belong to the same request: the values of
    :data:`MASKED_HEADERS` are pseudonymized **in place**, after the body and in
    the same session, so a path gets one token in both.

    ``session``, an :meth:`~privyx.core.engine.PrivacyEngine.ephemeral_session`,
    is used as is: no lock, no vault read or write.  Otherwise ``session_id`` is
    held through :meth:`~privyx.core.engine.PrivacyEngine.session_scope`.

    The caller's ``payload`` is never mutated (the walk rebuilds every container
    it descends into; config values are shared, not copied).

    With ``engine.directive``, a chat request of ``schema`` also gets
    :func:`directive`, unmasked (see :func:`_add_directive`; ``path`` tells a
    Gemini ``countTokens`` call, which takes none of its own).  With
    ``engine.cache_control``, an Anthropic one gets :func:`add_cache_control`.
    """

    def rank(key: str) -> int:
        return len(_PREFIX_KEYS) + 1 if key in METADATA_KEYS else _prefix_rank(key)

    rebuilt: dict[str, Any] = {}
    masked: set[str] = set()  # what this request masked, for engine.dlp_check
    scope: AbstractAsyncContextManager[Session] = (
        engine.session_scope(session_id) if session is None else nullcontext(session)
    )
    async with scope as held:

        async def transform(text: str) -> str:
            # `{"password": "hunter2"}`: a tool argument may be a secret by its key.
            key = text.key if isinstance(text, _Keyed) else None
            result = await engine.transform(str(text), session=held, persist=False, key=key)
            if engine.dlp_check:
                masked.update(t.original for t in result.transformations)
            return result.text

        async def metadata(text: str) -> str:
            return await transform(_unescaped(text))

        content_keys = GEMINI_CONTENT_KEYS if schema == "gemini" else CONTENT_KEYS
        for key in sorted(payload, key=rank):  # stable: the rest keep their order
            value = payload[key]
            if key == "tools":
                rebuilt[key] = await _drive(_descriptions(value), transform)
            elif key in content_keys:
                rebuilt[key] = await _drive(_content(value, key, schema), transform)
            elif key in METADATA_KEYS:
                rebuilt[key] = await _drive(_walk(value, key, True), metadata)
            else:
                rebuilt[key] = value
        if headers:
            for name in MASKED_HEADERS & headers.keys():
                headers[name] = _header_value(await metadata(headers[name]))
        if params is not None:
            for idx, (name, val) in enumerate(list(params)):
                if name.lower() not in AUTH_QUERY_PARAMS:
                    params[idx] = (name, await metadata(val))
        # Checked as it will be sent: signed thinking text is put back first.
        _pin_thinking(rebuilt.get("messages"))
        if masked:
            sent = [
                rebuilt,
                [v for k, v in (headers or {}).items() if k.lower() not in AUTH_HEADERS],
            ]
            sent += [[k, v] for k, v in params or [] if k.lower() not in AUTH_QUERY_PARAMS]
            leaks = find_leaks(sent, masked)
            if leaks:
                raise LeakError(f"{leaks} masked value(s) still in the request")
    result = {key: rebuilt[key] for key in payload}
    if engine.directive:
        _add_directive(result, schema, directive(engine.codec), path)
    if engine.cache_control and schema == "anthropic":
        add_cache_control(result)
    return result


#: Characters of prefix worth a cache breakpoint: about 1024 tokens at four
#: characters a token, the smallest minimum most Claude models cache.
CACHE_MIN_CHARS = 4096
_EPHEMERAL = {"type": "ephemeral"}


def add_cache_control(payload: dict[str, Any]) -> None:
    """Mark a large ``tools`` / ``system`` prefix of an Anthropic request for caching, in place.

    Only a request that marks nothing itself is changed: then its last tool
    gets a breakpoint when ``tools`` is at least :data:`CACHE_MIN_CHARS` long,
    and its last ``system`` block when ``tools`` and ``system`` together are.
    Both are the same on every turn, so the request stays byte-stable.  A
    client that sets ``cache_control`` anywhere manages caching itself.
    """
    if "messages" not in payload or _has_cache_control(payload):
        return
    tools = payload.get("tools")
    size = 0
    if isinstance(tools, list) and tools:
        size = len(json.dumps(tools, ensure_ascii=False))
        if size >= CACHE_MIN_CHARS and isinstance(tools[-1], dict):
            tools[-1] = {**tools[-1], "cache_control": dict(_EPHEMERAL)}
    system = payload.get("system")
    if isinstance(system, str) and system:
        system = [{"type": "text", "text": system}]
    if not isinstance(system, list) or not system or not isinstance(system[-1], dict):
        return
    size += sum(len(str(block.get("text", ""))) for block in system if isinstance(block, dict))
    if size >= CACHE_MIN_CHARS:
        payload["system"] = [*system[:-1], {**system[-1], "cache_control": dict(_EPHEMERAL)}]


def _has_cache_control(value: Any) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(v) for v in value)
    return False


def directive(codec: TokenCodec) -> str:
    """The instruction ``proxy.directive`` adds: keep placeholders exactly as written."""
    example = codec.encode(LogicalToken(namespace=codec.namespace, type="EMAIL", identifier="1"))
    return (
        "Privacy notice from the Privyx proxy: some values in this conversation are "
        f"replaced by placeholders such as {example}. Treat each placeholder as the value "
        "itself and copy it exactly as written, brackets included, wherever that value is "
        "needed: in replies, code, commands, file contents, and tool calls. Do not change, "
        "split, encode, translate, or guess what is behind a placeholder."
    )


def _add_directive(
    payload: dict[str, Any], schema: str | None, text: str, path: str | None = None
) -> None:
    """Add ``text`` to a chat request's system prompt, in place.

    It goes where it stays at one place turn after turn, so the provider's
    prompt cache keeps working: after Anthropic's ``system`` (the first block
    may have to stay first), after the leading system messages of a Chat
    request, and first in a Responses ``input``.  A request that resumes a
    stored response (``previous_response_id``) sends only its new items, so
    there it goes in ``instructions``, which is not carried over.  Gemini's
    goes last in ``systemInstruction``, also of the request a ``countTokens``
    call wraps; see :func:`_add_gemini_directive`.  Ollama gets none: a
    system prompt there replaces the one the model was made with.
    """
    if schema == "gemini":
        if (path or "").endswith(":countTokens"):
            nested = [payload.get(key) for key in _GEMINI_WRAPPED]
            for request in nested:
                if isinstance(request, dict):
                    _add_gemini_directive(request, text)
        else:
            _add_gemini_directive(payload, text)
        return
    if schema == "anthropic" and "messages" in payload:
        system = payload.get("system")
        if isinstance(system, list):
            payload["system"] = [*system, {"type": "text", "text": text}]
        else:
            payload["system"] = f"{system}\n\n{text}" if system else text
    elif schema == "openai" and isinstance(payload.get("messages"), list):
        messages = payload["messages"]
        at = 0
        while at < len(messages) and _role(messages[at]) in ("system", "developer"):
            at += 1
        messages.insert(at, {"role": "system", "content": text})
    elif schema == "responses" and "input" in payload:
        instructions = payload.get("instructions")
        if payload.get("previous_response_id"):
            payload["instructions"] = f"{instructions}\n\n{text}" if instructions else text
            return
        items = payload["input"]
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        message = {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        }
        payload["input"] = [message, *items] if isinstance(items, list) else items


def _add_gemini_directive(request: dict[str, Any], text: str) -> None:
    """Add ``text`` to a Gemini request's ``systemInstruction``, in place.

    Only to a request with ``contents``, and not to one that uses a cached
    content: Gemini refuses a ``systemInstruction`` beside it (the directive
    was added to the cache when it was created through Privyx).
    """
    if "contents" not in request or "cachedContent" in request or "cached_content" in request:
        return
    key = "system_instruction" if "system_instruction" in request else "systemInstruction"
    system = request.get(key)
    if system is None:
        request[key] = {"parts": [{"text": text}]}
    elif isinstance(system, dict) and isinstance(system.get("parts"), list):
        request[key] = {**system, "parts": [*system["parts"], {"text": text}]}


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else None


def find_leaks(value: Any, masked: set[str]) -> int:
    """How many of the ``masked`` values are still in ``value``'s strings.

    The check behind ``proxy.dlp_check``: a value masked in one place of a
    request but sent as is in another (a field that is not walked, a URL, a
    header, signed thinking text) is found here, and the request is refused.  Keys count as strings,
    and a value that starts or ends with a letter or digit must stand as a
    whole word there (``Bob`` is not found in ``Bobby``).
    """
    sent = "\0".join(_strings(value))
    # A plain substring search first: most values are not there at all.
    words = sorted((w for w in masked if w in sent), key=len, reverse=True)
    if not words:
        return 0
    pattern = re.compile("|".join(_whole(word) for word in words))  # the longest first
    return len(set(pattern.findall(sent)))


def _whole(word: str) -> str:
    left = r"(?<!\w)" if word[0].isalnum() else ""
    right = r"(?!\w)" if word[-1].isalnum() else ""
    return f"{left}{re.escape(word)}{right}"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)  # a masked tool-argument number, sent as one elsewhere
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


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


def remember_thinking(signature: Any, text: str) -> None:
    """Record the thinking ``text`` the upstream signed with ``signature``.

    The oldest entries go first once there are more than :data:`_THINKING_MAX`
    of them or they hold more than :data:`_THINKING_MAX_CHARS` characters.
    """
    global _THINKING_CHARS
    if not isinstance(signature, str) or not signature:
        return
    # Resync when a caller (e.g. tests monkeypatching _THINKING) clears the dict directly.
    if not _THINKING:
        _THINKING_CHARS = 0
    if signature in _THINKING:
        _THINKING_CHARS -= len(signature) + len(_THINKING[signature])
    _THINKING[signature] = text
    _THINKING_CHARS += len(signature) + len(text)
    while len(_THINKING) > _THINKING_MAX or _THINKING_CHARS > _THINKING_MAX_CHARS:
        oldest = next(iter(_THINKING))
        _THINKING_CHARS -= len(oldest) + len(_THINKING.pop(oldest))


def _pin_thinking(messages: Any) -> None:
    """Put the signed upstream text back into each echoed ``thinking`` block.

    ``messages`` is the walk's rebuilt copy, so mutating it is safe.
    """
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                original = _THINKING.get(block.get("signature") or "")
                if original is not None:
                    block["thinking"] = original


async def restore_response(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
    *,
    session: Session | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Return a copy of a batch ``payload`` with pseudonyms restored.

    Every string leaf is restored whatever the route's schema (OpenAI-compatible
    gateways often answer ``/v1/messages`` with a ``chat.completion`` body); only
    opaque keys are skipped, so a token-shaped id is left alone.  ``schema``
    adds its own document and opaque keys (Gemini's ``functionCall.args``).
    ``session``, when the caller holds it, skips the vault read.  When the
    session is unknown the payload is returned untouched.
    """
    content = payload.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "thinking":
            remember_thinking(block.get("signature"), str(block.get("thinking", "")))
    if session is None:
        session = await engine.vault.get(session_id)
    if session is None:
        return payload

    async def restore(text: str) -> str:
        return (await engine.restore(text, session_id, session=session)).text

    result: dict[str, Any] = await walk_async(payload, restore, schema=schema)
    return result


def walk_sync(
    value: Any, fn: Callable[[str], str], key: str | None = None, *, schema: str | None = None
) -> Any:
    """Rebuild ``value`` with ``fn`` applied to each content string leaf."""
    walker = _walk(value, key, schema=schema)
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
    schema: str | None = None,
) -> Any:
    """Async twin of :func:`walk_sync` for the engine's ``transform``/``restore``."""
    return await _drive(_walk(value, key, schema=schema), fn)


async def _drive(walker: _Walk, fn: Callable[[str], Awaitable[str]]) -> Any:
    """Feed each leaf ``walker`` yields through ``fn``; return the rebuilt value."""
    try:
        text = next(walker)
        while True:
            text = walker.send(await fn(text))
    except StopIteration as done:
        return done.value


def _walk(
    value: Any,
    key: str | None,
    document: bool = False,
    *,
    schema: str | None = None,
    keyed: bool = False,
) -> _Walk:
    """Yield every content string leaf of ``value`` and return the rebuilt value.

    ``key`` is the key ``value`` sits under.  A tool-argument *document* — an
    object under ``input`` (Anthropic ``tool_use``/``server_tool_use``) or
    ``arguments``, or the JSON a string ``arguments`` holds; for ``schema``
    ``gemini`` an object under ``args`` or ``response`` — is user data with
    no wire structure, so inside it no key is opaque (a ``name`` argument is
    PII).  A string ``arguments`` is walked parsed and re-serialized, never as
    one text blob (a match could straddle a JSON escape); unparseable arguments
    are walked as plain text, never passed through raw.

    With ``keyed`` (masking), a string in a document under a key, also from
    inside a list, is yielded as a :class:`_Keyed`: a value may only say what
    it is with its key.  A number in a document is yielded as text, alone
    (with its key, ``{"token_limit": 4000}`` would read as a secret), and goes
    back as it was unless masked.
    """
    if isinstance(value, str):
        if not value:
            return value
        if key == "arguments":
            try:
                parsed = json.loads(value)
            except ValueError:
                return (yield value)
            rebuilt = yield from _walk(parsed, None, True, schema=schema, keyed=keyed)
            return value if rebuilt == parsed else json.dumps(rebuilt, ensure_ascii=False)
    if keyed and document and key is not None and (text := leaf_text(value)) is not None:
        leaf = _Keyed(text, key) if isinstance(value, str) else text
        return put_back(value, text, (yield leaf))
    if isinstance(value, str):
        return (yield value)
    if isinstance(value, list):
        items = []
        for item in value:
            items.append((yield from _walk(item, key, document, schema=schema, keyed=keyed)))
        return items
    if isinstance(value, dict):
        documents = _DOCUMENT_KEYS.get(schema, _DEFAULT_DOCUMENT_KEYS)
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not document and _opaque(k, value, schema):
                out[k] = v
            else:
                nested = document or (k in documents and isinstance(v, dict))
                out[k] = yield from _walk(v, k, nested, schema=schema, keyed=keyed)
        return out
    return value


def _prefix_rank(key: str) -> int:
    """Where ``key`` goes in the walk: :data:`_PREFIX_KEYS` first, in their order."""
    return _PREFIX_KEYS.index(key) if key in _PREFIX_KEYS else len(_PREFIX_KEYS)


def _content(value: Any, key: str, schema: str | None) -> _Walk:
    """Walk the content ``value`` of a request's top-level ``key``.

    The request a Gemini ``countTokens`` call wraps is walked as a request:
    its content keys and ``tools`` descriptions, nothing else.
    """
    if schema == "gemini" and key in _GEMINI_WRAPPED and isinstance(value, dict):
        return (yield from _gemini_request(value))
    return (yield from _walk(value, key, schema=schema, keyed=True))


def _gemini_request(value: dict[str, Any]) -> _Walk:
    out: dict[str, Any] = {}
    for k in sorted(value, key=_prefix_rank):
        v = value[k]
        if k == "tools":
            out[k] = yield from _descriptions(v)
        elif k in GEMINI_CONTENT_KEYS:
            out[k] = yield from _content(v, k, "gemini")
        else:
            out[k] = v
    return {k: out[k] for k in value}


def _descriptions(value: Any, key: str | None = None) -> _Walk:
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


def _opaque(key: str, parent: Mapping[str, Any], schema: str | None = None) -> bool:
    """Whether ``key`` (in the object ``parent``) is structural, not content."""
    if key == "data" and parent.get("type") == "text":
        return False  # Anthropic plain-text document source: the document itself
    if key == "result" and parent.get("type") == "image_generation_call":
        return True  # base64 image, echoed back in a Responses input
    if key in _SCHEMA_OPAQUE_KEYS.get(schema, ()):
        return True
    return key in OPAQUE_KEYS or key.endswith(("_id", "_url")) or key.startswith("encrypted_")
