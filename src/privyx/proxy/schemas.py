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
  server writes those, and they reach the model on every turn.

Two things keep a multi-turn conversation byte-stable, which prompt caching and
Anthropic's thinking signatures both need: the request is walked in cache-prefix
order (``tools``, then ``system`` / ``instructions``, then the rest), so a value
first seen in a later message cannot renumber the system prompt; and an echoed
``thinking`` block gets back the exact text the upstream signed
(:func:`remember_thinking`) instead of a re-pseudonymization of its restored form.

Streaming responses are handled by
:class:`~privyx.proxy.stream_router.StreamRouter`, which reuses the same walk
for every event it does not restore delta by delta.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.core.session import Session

#: The schemas whose request/response bodies this module knows how to walk.
KNOWN_SCHEMAS: frozenset[str] = frozenset({"openai", "anthropic", "responses"})

#: Top-level request keys that carry conversation content.  ``messages`` and
#: ``system`` (Chat, Anthropic); ``input``, ``instructions``, and ``prompt``
#: (template variables) for Responses; ``prediction`` (Chat predicted output).
CONTENT_KEYS: frozenset[str] = frozenset(
    {"messages", "system", "input", "instructions", "prompt", "prediction"}
)

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

#: Top-level keys walked first, in the order the provider caches the prompt.
_PREFIX_KEYS: tuple[str, ...] = ("tools", "system", "instructions")

#: Thinking text exactly as the upstream sent it, by signature.  The client gets
#: it restored and echoes it back, but re-pseudonymizing that is not always the
#: exact inverse (a value the model wrote itself is new to the detector), and the
#: provider rejects a thinking block whose text no longer matches its signature.
# ponytail: process-local FIFO — lost on restart, not shared between workers;
# move it into the vault if multi-instance deployments hit signature errors.
_THINKING: dict[str, str] = {}
_THINKING_MAX = 4096
#: Characters held in all, signatures included: bounded by count alone, 4096
#: long thinking blocks could hold hundreds of MB.
_THINKING_MAX_CHARS = 16_000_000

# A generator that yields each string leaf, receives its replacement, and
# returns the rebuilt value.  Written once as a generator so the async
# transform/restore and the synchronous stream restore share one walk.
_Walk = Generator[str, str, Any]


def detect_schema(path: str, routes: dict[str, str]) -> str | None:
    """Return the wire schema configured for ``path``, or ``None``.

    ``path`` is matched with a single leading slash, so both ``v1/messages``
    (as FastAPI hands it to a ``{path:path}`` route) and ``/v1/messages`` resolve
    the same way.
    """
    normalized = "/" + path.lstrip("/")
    return routes.get(normalized)


async def transform_request(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
    *,
    session: Session | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with its content leaves pseudonymized.

    Only :data:`CONTENT_KEYS` are walked; inside them every string leaf is
    pseudonymized except the opaque keys (see :func:`_opaque`), including the
    assistant turns a client echoes back — a restored value that is not
    pseudonymized again would reach the upstream in the clear.

    ``session``, an :meth:`~privyx.core.engine.PrivacyEngine.ephemeral_session`,
    is used as is: no lock, no vault read or write.  Otherwise ``session_id`` is
    held through :meth:`~privyx.core.engine.PrivacyEngine.session_scope`.

    The caller's ``payload`` is never mutated (the walk rebuilds every container
    it descends into; config values are shared, not copied).
    """

    def rank(key: str) -> int:
        return _PREFIX_KEYS.index(key) if key in _PREFIX_KEYS else len(_PREFIX_KEYS)

    rebuilt: dict[str, Any] = {}
    scope: AbstractAsyncContextManager[Session] = (
        engine.session_scope(session_id) if session is None else nullcontext(session)
    )
    async with scope as held:

        async def transform(text: str) -> str:
            return (await engine.transform(text, session=held, persist=False)).text

        for key in sorted(payload, key=rank):  # stable: the rest keep their order
            value = payload[key]
            if key == "tools":
                rebuilt[key] = await _drive(_descriptions(value), transform)
            elif key in CONTENT_KEYS:
                rebuilt[key] = await walk_async(value, transform, key)
            else:
                rebuilt[key] = value
    _pin_thinking(rebuilt.get("messages"))
    return {key: rebuilt[key] for key in payload}


def remember_thinking(signature: Any, text: str) -> None:
    """Record the thinking ``text`` the upstream signed with ``signature``.

    The oldest entries go first once there are more than :data:`_THINKING_MAX`
    of them or they hold more than :data:`_THINKING_MAX_CHARS` characters.
    """
    if not isinstance(signature, str) or not signature:
        return
    _THINKING[signature] = text
    total = sum(len(key) + len(value) for key, value in _THINKING.items())
    while len(_THINKING) > _THINKING_MAX or total > _THINKING_MAX_CHARS:
        oldest = next(iter(_THINKING))
        total -= len(oldest) + len(_THINKING.pop(oldest))


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
) -> dict[str, Any]:
    """Return a copy of a batch ``payload`` with pseudonyms restored.

    Every string leaf is restored whatever the route's schema (OpenAI-compatible
    gateways often answer ``/v1/messages`` with a ``chat.completion`` body); only
    opaque keys are skipped, so a token-shaped id is left alone.  ``session``,
    when the caller holds it, skips the vault read.  When the session is unknown
    the payload is returned untouched.
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

    result: dict[str, Any] = await walk_async(payload, restore)
    return result


def walk_sync(value: Any, fn: Callable[[str], str], key: str | None = None) -> Any:
    """Rebuild ``value`` with ``fn`` applied to each content string leaf."""
    walker = _walk(value, key)
    try:
        text = next(walker)
        while True:
            text = walker.send(fn(text))
    except StopIteration as done:
        return done.value


async def walk_async(
    value: Any, fn: Callable[[str], Awaitable[str]], key: str | None = None
) -> Any:
    """Async twin of :func:`walk_sync` for the engine's ``transform``/``restore``."""
    return await _drive(_walk(value, key), fn)


async def _drive(walker: _Walk, fn: Callable[[str], Awaitable[str]]) -> Any:
    """Feed each leaf ``walker`` yields through ``fn``; return the rebuilt value."""
    try:
        text = next(walker)
        while True:
            text = walker.send(await fn(text))
    except StopIteration as done:
        return done.value


def _walk(value: Any, key: str | None, document: bool = False) -> _Walk:
    """Yield every content string leaf of ``value`` and return the rebuilt value.

    ``key`` is the key ``value`` sits under.  A tool-argument *document* — an
    object under ``input`` (Anthropic ``tool_use``/``server_tool_use``) or
    ``arguments``, or the JSON a string ``arguments`` holds — is user data with
    no wire structure, so inside it no key is opaque (a ``name`` argument is
    PII).  A string ``arguments`` is walked parsed and re-serialized, never as
    one text blob (a match could straddle a JSON escape); unparseable arguments
    are walked as plain text, never passed through raw.
    """
    if isinstance(value, str):
        if not value:
            return value
        if key == "arguments":
            try:
                parsed = json.loads(value)
            except ValueError:
                return (yield value)
            rebuilt = yield from _walk(parsed, None, True)
            return value if rebuilt == parsed else json.dumps(rebuilt, ensure_ascii=False)
        return (yield value)
    if isinstance(value, list):
        items = []
        for item in value:
            items.append((yield from _walk(item, key, document)))
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not document and _opaque(k, value):
                out[k] = v
            else:
                nested = document or (k in ("input", "arguments") and isinstance(v, dict))
                out[k] = yield from _walk(v, k, nested)
        return out
    return value


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


def _opaque(key: str, parent: Mapping[str, Any]) -> bool:
    """Whether ``key`` (in the object ``parent``) is structural, not content."""
    if key == "data" and parent.get("type") == "text":
        return False  # Anthropic plain-text document source: the document itself
    if key == "result" and parent.get("type") == "image_generation_call":
        return True  # base64 image, echoed back in a Responses input
    return key in OPAQUE_KEYS or key.endswith(("_id", "_url")) or key.startswith("encrypted_")
