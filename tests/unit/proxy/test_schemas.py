"""Unit tests for wire-schema detection and structural transforms."""

from __future__ import annotations

import json
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.schemas import detect_schema, restore_response, transform_request
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
PHONE = "+1-555-0142"
ROUTES = {"/v1/chat/completions": "openai", "/v1/messages": "anthropic"}


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


async def _session(engine: PrivacyEngine) -> str:
    return (await engine.get_or_create_session()).session_id


def _mail_request(email: str = EMAIL) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": f"mail {email}"}]}


def test_detect_schema_normalizes_leading_slash() -> None:
    assert detect_schema("v1/chat/completions", ROUTES) == "openai"
    assert detect_schema("/v1/messages", ROUTES) == "anthropic"
    assert detect_schema("v1/models", ROUTES) is None


async def test_transform_request_openai_str_and_parts_no_mutation() -> None:
    engine = _engine()
    sid = await _session(engine)
    payload = {
        "messages": [
            {"role": "user", "content": f"mail {EMAIL}"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"phone {PHONE}"},
                    {"type": "image", "url": "https://x/y.png"},
                ],
            },
        ]
    }
    original = json.dumps(payload)

    out = await transform_request(payload, engine, sid)

    serialized = json.dumps(out)
    assert EMAIL not in serialized and PHONE not in serialized and "<PRIVYX_" in serialized
    assert out["messages"][1]["content"][1]["url"] == "https://x/y.png"
    assert json.dumps(payload) == original  # caller payload untouched


async def test_transform_request_anthropic_system_list_and_str() -> None:
    engine = _engine()
    sid = await _session(engine)

    listed = await transform_request(
        {"system": [{"type": "text", "text": f"op {EMAIL}"}], "messages": []}, engine, sid
    )
    assert EMAIL not in json.dumps(listed)
    assert "<PRIVYX_" in listed["system"][0]["text"]

    stringy = await transform_request({"system": f"op {EMAIL}", "messages": []}, engine, sid)
    assert EMAIL not in stringy["system"]


async def test_restore_openai_content_and_tool_arguments() -> None:
    engine = _engine()
    sid = await _session(engine)
    await transform_request(_mail_request(), engine, sid)
    session = await engine.vault.get(sid)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)
    assert pseudonym is not None

    response = {
        "choices": [
            {
                "message": {
                    "content": f"ok {pseudonym}",
                    "tool_calls": [
                        {"function": {"name": "send", "arguments": json.dumps({"to": pseudonym})}}
                    ],
                }
            }
        ]
    }
    out = await restore_response(response, engine, sid)

    message = out["choices"][0]["message"]
    assert message["content"] == f"ok {EMAIL}"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"])["to"] == EMAIL


async def test_restore_anthropic_text_and_tool_input() -> None:
    engine = _engine()
    sid = await _session(engine)
    await transform_request(_mail_request(), engine, sid)
    session = await engine.vault.get(sid)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)

    response = {
        "content": [
            {"type": "text", "text": f"hi {pseudonym}"},
            {"type": "tool_use", "input": {"to": pseudonym}},
        ]
    }
    out = await restore_response(response, engine, sid)

    assert out["content"][0]["text"] == f"hi {EMAIL}"
    assert out["content"][1]["input"]["to"] == EMAIL


async def test_restore_unknown_session_is_passthrough() -> None:
    engine = _engine()
    response = {"choices": [{"message": {"content": "<PRIVYX_EMAIL_1>"}}]}
    out = await restore_response(response, engine, "ses_missing")
    assert out["choices"][0]["message"]["content"] == "<PRIVYX_EMAIL_1>"


async def test_restore_openai_shaped_body_on_anthropic_route() -> None:
    # OpenAI-compatible gateways answer /v1/messages with a chat.completion body.
    engine = _engine()
    sid = await _session(engine)
    await transform_request(_mail_request(), engine, sid)
    session = await engine.vault.get(sid)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)

    response = {
        "object": "chat.completion",
        "choices": [{"message": {"content": f"hi {pseudonym}"}}],
    }
    out = await restore_response(response, engine, sid)

    assert out["choices"][0]["message"]["content"] == f"hi {EMAIL}"
