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
  (:data:`CONTENT_KEYS`) — everything else there is config (``model``, ``tools``,
  ``response_format``, …) and is forwarded as-is — and inside them pseudonymizes
  every string leaf except opaque/structural keys (:func:`_opaque`).  It fails
  closed: an unknown field is pseudonymized, never forwarded raw.

Streaming responses are handled by
:class:`~privyx.proxy.stream_router.StreamRouter`, which reuses the same walk
for every event it does not restore delta by delta.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator, Mapping
from typing import Any

from privyx.core.engine import PrivacyEngine

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
) -> dict[str, Any]:
    """Return a copy of ``payload`` with its content leaves pseudonymized.

    Only :data:`CONTENT_KEYS` are walked; inside them every string leaf is
    pseudonymized except the opaque keys (see :func:`_opaque`), including the
    assistant turns a client echoes back — a restored value that is not
    pseudonymized again would reach the upstream in the clear.

    The caller's ``payload`` is never mutated (the walk rebuilds every container
    it descends into; config values are shared, not copied).
    """

    async def transform(text: str) -> str:
        return (await engine.transform(text, session_id=session_id)).text

    return {
        key: await walk_async(value, transform, key) if key in CONTENT_KEYS else value
        for key, value in payload.items()
    }


async def restore_response(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
) -> dict[str, Any]:
    """Return a copy of a batch ``payload`` with pseudonyms restored.

    Every string leaf is restored whatever the route's schema (OpenAI-compatible
    gateways often answer ``/v1/messages`` with a ``chat.completion`` body); only
    opaque keys are skipped, so a token-shaped id is left alone.  When the
    session is unknown the payload is returned untouched.
    """
    if await engine.vault.get(session_id) is None:
        return payload

    # ponytail: one vault read per leaf (engine.restore re-fetches the session);
    # fetch once and deanonymize through the operator if large bodies profile hot.
    async def restore(text: str) -> str:
        return (await engine.restore(text, session_id)).text

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
    walker = _walk(value, key)
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


def _opaque(key: str, parent: Mapping[str, Any]) -> bool:
    """Whether ``key`` (in the object ``parent``) is structural, not content."""
    if key == "data" and parent.get("type") == "text":
        return False  # Anthropic plain-text document source: the document itself
    if key == "result" and parent.get("type") == "image_generation_call":
        return True  # base64 image, echoed back in a Responses input
    return (
        key in OPAQUE_KEYS
        or key.endswith(("_id", "_url"))
        or key.startswith("encrypted_")
    )
