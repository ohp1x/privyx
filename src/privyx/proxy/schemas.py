"""Wire-schema routing and structural transforms.

A *wire schema* is the JSON shape a provider speaks on a given path.  The
transparent proxy needs to know, per request, three things:

1. which path maps to which schema (:func:`detect_schema`);
2. where the sensitive *text leaves* live in a request body, so they can be
   pseudonymized before forwarding (:func:`transform_request`);
3. where the assistant's text lives in a batch response, so it can be restored
   (:func:`restore_response`).

Keeping this here — rather than as ``if openai / elif anthropic`` branches
scattered through the proxy — is what lets one catch-all route serve both
providers.  Streaming responses are handled separately by
:class:`~privyx.proxy.stream_router.StreamRouter`, which needs event-by-event
classification this module does not.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from privyx.core.engine import PrivacyEngine

#: The schemas whose request/response bodies this module knows how to walk.
KNOWN_SCHEMAS: frozenset[str] = frozenset({"openai", "anthropic"})


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
    """Return a copy of ``payload`` with sensitive text leaves pseudonymized.

    Covers the fields common to the OpenAI and Anthropic request bodies:

    - ``messages[].content`` — a string, or a list of ``{"type": "text", ...}``
      content parts / blocks;
    - ``system`` — a string (OpenAI top-level / Anthropic) or a list of text
      blocks (Anthropic).

    The caller's ``payload`` is never mutated.
    """
    result = copy.deepcopy(payload)

    messages = result.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                message["content"] = await _transform_content(
                    message.get("content"), engine, session_id
                )

    system = result.get("system")
    if system is not None:
        result["system"] = await _transform_content(system, engine, session_id)

    return result


async def restore_response(
    payload: dict[str, Any],
    engine: PrivacyEngine,
    session_id: str,
) -> dict[str, Any]:
    """Return a copy of a batch ``payload`` with pseudonyms restored.

    Restoration is precise — only known text leaves are rewritten, so a token
    that happens to appear in an id or other structural field is left alone
    (unlike a blunt whole-body string replace).  When the session is unknown
    the payload is returned untouched.

    Both response shapes are walked regardless of the route's schema: OpenAI-
    compatible gateways often answer ``/v1/messages`` with a ``chat.completion``
    body.  Each walker is a no-op when its shape is absent.
    """
    session = await engine.vault.get(session_id)
    if session is None:
        return payload
    result = copy.deepcopy(payload)
    await _restore_anthropic(result, engine, session_id)
    await _restore_openai(result, engine, session_id)
    return result


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


async def _transform_text(text: str, engine: PrivacyEngine, session_id: str) -> str:
    result = await engine.transform(text, session_id=session_id)
    return result.text


async def _transform_content(content: Any, engine: PrivacyEngine, session_id: str) -> Any:
    """Transform a ``content`` value: a string, or a list of text parts/blocks."""
    if isinstance(content, str):
        return await _transform_text(content, engine, session_id)
    if isinstance(content, list):
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                part["text"] = await _transform_text(part["text"], engine, session_id)
        return content
    return content


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


async def _restore_text(text: str, engine: PrivacyEngine, session_id: str) -> str:
    result = await engine.restore(text, session_id)
    return result.text


async def _restore_openai(
    payload: dict[str, Any], engine: PrivacyEngine, session_id: str
) -> None:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            delta = choice.get("delta")
            message = delta if isinstance(delta, dict) else None
        if isinstance(message, dict):
            await _restore_openai_message(message, engine, session_id)


async def _restore_openai_message(
    message: dict[str, Any], engine: PrivacyEngine, session_id: str
) -> None:
    for field in ("content", "reasoning_content"):
        value = message.get(field)
        if isinstance(value, str):
            message[field] = await _restore_text(value, engine, session_id)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = await _restore_text(
                    function["arguments"], engine, session_id
                )


async def _restore_anthropic(
    payload: dict[str, Any], engine: PrivacyEngine, session_id: str
) -> None:
    content = payload.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            block["text"] = await _restore_text(block["text"], engine, session_id)
        if block.get("type") == "tool_use" and isinstance(block.get("input"), (dict, list)):
            block["input"] = await _restore_json_value(block["input"], engine, session_id)


async def _restore_json_value(value: Any, engine: PrivacyEngine, session_id: str) -> Any:
    """Restore pseudonyms inside a JSON value by round-tripping through text.

    Tool inputs are structured JSON; serializing, restoring, and re-parsing
    reaches string leaves at any depth without walking the tree by hand.  If the
    restored text is somehow no longer valid JSON the original value is kept.
    """
    raw = json.dumps(value, ensure_ascii=False)
    restored = await _restore_text(raw, engine, session_id)
    try:
        return json.loads(restored)
    except (json.JSONDecodeError, ValueError):
        return value
