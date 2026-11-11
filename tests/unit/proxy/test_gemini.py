"""Gemini wire schema: route patterns, request masking, and reply restoring."""

from __future__ import annotations

import json
from typing import Any

from privyx.config.schema import ProxyConfig
from privyx.core.engine import PrivacyEngine
from privyx.observability.usage import UsageMeter
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.schemas import detect_schema, restore_response, transform_request
from privyx.proxy.session import resolve_session_id
from privyx.proxy.stream_router import StreamRouter, restore_chunks
from privyx.streaming.adapters.registry import build_stream_adapter
from privyx.streaming.sse import SSEDecoder
from privyx.token.codec import FormatCodec
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
TOKEN = "<PRIVYX_EMAIL_1>"
ROUTES = ProxyConfig().routes


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


async def _mask(payload: dict[str, Any]) -> tuple[dict[str, Any], PrivacyEngine, str]:
    engine = _engine()
    sid = (await engine.get_or_create_session()).session_id
    return await transform_request(payload, engine, sid, schema="gemini"), engine, sid


# -- routes ----------------------------------------------------------------


def test_default_routes_match_gemini_paths_with_any_model() -> None:
    for path in (
        "/v1beta/models/gemini-2.5-flash:generateContent",
        "v1beta/models/gemini-2.5-pro:streamGenerateContent",
        "/v1/models/gemini-2.0-flash-001:countTokens",
        "/v1beta/models/text-embedding-004:embedContent",
        "/v1beta/models/gemini-embedding-001:batchEmbedContents",
        "/v1alpha/models/gemini-2.5-flash:generateContent",
        "/v1beta/tunedModels/my-model-123:streamGenerateContent",
        "/v1beta/cachedContents",
    ):
        assert detect_schema(path, ROUTES) == "gemini"
    for path in (
        "/v1beta/models/:generateContent",  # no model
        "/v1beta/models/a/b:generateContent",  # `*` stays within a segment
        "/v1beta/models/gemini-2.5-flash:predict",
        "/v1beta/models/gemini-2.5-flash:generateContentX",
    ):
        assert detect_schema(path, ROUTES) is None


def test_a_route_without_a_star_wins_and_the_first_pattern_next() -> None:
    routes = {
        "/v1beta/models/*:generateContent": "gemini",
        "/v1beta/models/special:generateContent": "openai",
        "/v1beta/*/x:generateContent": "anthropic",
        "/v1beta/models/*": "responses",
    }
    assert detect_schema("/v1beta/models/special:generateContent", routes) == "openai"
    assert detect_schema("/v1beta/models/other:generateContent", routes) == "gemini"
    assert detect_schema("/v1beta/models/other", routes) == "responses"
    # Regex characters in a route are literal.
    assert detect_schema("/v1beta/models/x.generateContent", {"/v1beta/models/x:*": "x"}) is None


# -- request ---------------------------------------------------------------


async def test_request_masks_conversation_and_keeps_wire_structure() -> None:
    signature = "c2lnbmF0dXJl" + TOKEN  # restored or masked, it would no longer verify
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": f"mail {EMAIL}"},
                    {"inlineData": {"mimeType": "image/png", "data": EMAIL}},
                    {"fileData": {"mimeType": "text/plain", "fileUri": f"https://x.test/{EMAIL}"}},
                ],
            },
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "send",
                            "id": "call-1",
                            "args": {"name": EMAIL, "to": [EMAIL], "type": EMAIL},
                        },
                        "thoughtSignature": signature,
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "send",
                            "response": {"id": EMAIL, "result": f"sent to {EMAIL}"},
                        }
                    }
                ],
            },
        ],
        "system_instruction": {"parts": [{"text": f"owner {EMAIL}"}]},
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "send",
                        "description": f"mails {EMAIL}",
                        "parameters": {"type": "object", "enum": [EMAIL]},
                    }
                ]
            }
        ],
        "generationConfig": {"stopSequences": [EMAIL]},
        "cachedContent": "cachedContents/abc",
    }

    out, engine, sid = await _mask(payload)

    sent = json.dumps(out)
    contents = out["contents"]
    assert contents[0]["parts"][0]["text"] == f"mail {TOKEN}"
    assert contents[0]["parts"][1]["inlineData"] == {"mimeType": "image/png", "data": EMAIL}
    assert contents[0]["parts"][2]["fileData"]["fileUri"] == f"https://x.test/{EMAIL}"
    call = contents[1]["parts"][0]
    assert call["thoughtSignature"] == signature
    assert call["functionCall"]["name"] == "send"
    assert call["functionCall"]["args"] == {"name": TOKEN, "to": [TOKEN], "type": TOKEN}
    reply = contents[2]["parts"][0]["functionResponse"]["response"]
    assert reply == {"id": TOKEN, "result": f"sent to {TOKEN}"}
    assert out["system_instruction"] == {"parts": [{"text": f"owner {TOKEN}"}]}
    declaration = out["tools"][0]["functionDeclarations"][0]
    assert declaration["description"] == f"mails {TOKEN}"
    assert declaration["parameters"] == payload["tools"][0]["functionDeclarations"][0]["parameters"]
    assert out["generationConfig"] == payload["generationConfig"]
    assert sent.count(EMAIL) == 4  # inline data, file uri, enum, stop sequence
    # The tokens restore to the original.
    restored = await restore_response(out, engine, sid, schema="gemini")
    assert restored["contents"][1]["parts"][0]["functionCall"]["args"]["type"] == EMAIL
    assert restored["contents"] == {**payload}["contents"]


async def test_count_tokens_wrapping_a_request_is_masked_as_one() -> None:
    payload = {
        "generateContentRequest": {
            "model": "models/gemini-2.5-flash",
            "contents": [{"parts": [{"text": f"hi {EMAIL}"}]}],
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "tools": [{"functionDeclarations": [{"name": "f", "description": EMAIL}]}],
            "generationConfig": {"stopSequences": [EMAIL]},
        }
    }

    out, _, _ = await _mask(payload)

    request = out["generateContentRequest"]
    assert request["model"] == "models/gemini-2.5-flash"
    assert request["tools"][0]["functionDeclarations"][0] == {"name": "f", "description": TOKEN}
    assert request["contents"] == [{"parts": [{"text": f"hi {TOKEN}"}]}]
    assert request["generationConfig"] == {"stopSequences": [EMAIL]}
    assert list(request) == list(payload["generateContentRequest"])


async def test_other_schemas_keep_their_document_and_opaque_keys() -> None:
    """`args` / `response` / `mimeType` mean nothing special outside Gemini."""
    engine = _engine()
    sid = (await engine.get_or_create_session()).session_id
    payload = {"messages": [{"role": "user", "content": [{"args": {"name": EMAIL}}]}]}

    out = await transform_request(payload, engine, sid, schema="openai")

    assert out == payload  # `name` is opaque outside a tool-argument document


async def test_embedding_requests_mask_their_text_only() -> None:
    one = {"content": {"parts": [{"text": f"hi {EMAIL}"}]}, "taskType": "RETRIEVAL_DOCUMENT"}
    single = {**one, "title": f"note for {EMAIL}", "outputDimensionality": 8}
    batch = {"requests": [{"model": "models/text-embedding-004", **one}, "odd " + EMAIL]}

    out, _, _ = await _mask(single)
    assert out == {
        "content": {"parts": [{"text": f"hi {TOKEN}"}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "title": f"note for {TOKEN}",
        "outputDimensionality": 8,
    }
    out, _, _ = await _mask(batch)
    assert out["requests"] == [
        {
            "model": "models/text-embedding-004",
            **one,
            "content": {"parts": [{"text": f"hi {TOKEN}"}]},
        },
        f"odd {TOKEN}",
    ]


def test_conversation_session_reads_gemini_key_and_first_user_turn() -> None:
    def resolve(payload: dict[str, Any], key: str) -> tuple[str | None, str]:
        return resolve_session_id(
            "conversation",
            header_value=None,
            headers={"x-goog-api-key": key},
            payload=payload,
            schema="gemini",
        )

    first = {"contents": [{"role": "user", "parts": [{"text": "plan "}, {"text": "a trip"}]}]}
    later = {
        "contents": [
            *first["contents"],
            {"role": "model", "parts": [{"text": "ok"}]},
            {"role": "user", "parts": [{"text": "more"}]},
        ]
    }
    other = {"contents": [{"parts": [{"text": "another"}]}]}  # role may be left out

    sid, source = resolve(first, "k1")
    assert source == "conversation"
    assert resolve(later, "k1")[0] == sid
    assert resolve(other, "k1")[0] != sid
    assert resolve(first, "k2")[0] != sid


# -- stream ----------------------------------------------------------------


def _chunk(*parts: dict[str, Any], finish: str | None = None, index: int = 0) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}}
    if finish:
        candidate["finishReason"] = finish
    candidate["index"] = index
    return {"candidates": [candidate], "modelVersion": "gemini-2.5-flash"}


def _sse(*chunks: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)


def _run(body: str, mapping: dict[str, str] | None = None) -> list[dict[str, Any]]:
    router = StreamRouter(
        FormatCodec.default(), (mapping or {TOKEN: EMAIL}).get, build_stream_adapter("gemini")
    )
    out = "".join(router.feed(body.encode()) + router.flush())
    decoder = SSEDecoder()
    return [json.loads(e.data) for e in decoder.feed(out) + decoder.flush()]


def _texts(chunks: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    return [
        (part["text"], part.get("thought", False))
        for chunk in chunks
        for candidate in chunk.get("candidates", [])
        for part in candidate["content"]["parts"]
        if "text" in part
    ]


def test_stream_restores_a_token_split_across_chunks() -> None:
    body = _sse(
        _chunk({"text": "Hi " + TOKEN[:9]}),
        _chunk({"text": TOKEN[9:] + " bye"}, finish="STOP"),
    )

    chunks = _run(body)

    assert "".join(t for t, _ in _texts(chunks)) == f"Hi {EMAIL} bye"
    assert TOKEN not in json.dumps(chunks)
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
    assert all(c["modelVersion"] == "gemini-2.5-flash" for c in chunks)


def test_stream_drops_a_chunk_whose_text_is_all_held_back() -> None:
    body = _sse(_chunk({"text": TOKEN[:5]}), _chunk({"text": TOKEN[5:]}, finish="STOP"))

    chunks = _run(body)

    assert len(chunks) == 1
    assert _texts(chunks) == [(EMAIL, False)]


def test_stream_gives_back_held_text_before_a_function_call() -> None:
    call = {"functionCall": {"name": "send", "args": {"name": TOKEN, "to": TOKEN}}}
    body = _sse(
        _chunk({"text": "to " + TOKEN[:6]}),
        _chunk({"text": TOKEN[6:]}, call, finish="STOP"),
    )

    chunks = _run(body)

    parts = [p for c in chunks for p in c["candidates"][0]["content"]["parts"]]
    assert parts == [
        {"text": "to "},
        {"text": EMAIL},
        {"functionCall": {"name": "send", "args": {"name": EMAIL, "to": EMAIL}}},
    ]


def test_stream_gives_back_a_held_fragment_before_a_call_in_the_next_chunk() -> None:
    call = {"functionCall": {"name": "send", "args": {}}}
    body = _sse(_chunk({"text": "a <PRIV"}), _chunk(call), _chunk({"text": "b"}, finish="STOP"))

    chunks = _run(body)

    parts = [p for c in chunks for p in c["candidates"][0]["content"]["parts"]]
    assert parts == [{"text": "a "}, {"text": "<PRIV"}, call, {"text": "b"}]


def test_stream_finish_chunk_carries_the_held_fragment() -> None:
    chunks = _run(_sse(_chunk({"text": "ends with <PRIV"}, finish="STOP")))

    assert len(chunks) == 1
    assert chunks[0]["candidates"][0]["finishReason"] == "STOP"
    assert _texts(chunks) == [("ends with ", False), ("<PRIV", False)]


def test_stream_keeps_thought_and_text_apart() -> None:
    body = _sse(
        _chunk({"text": "think " + TOKEN[:4], "thought": True}),
        _chunk({"text": "answer " + TOKEN}, finish="STOP"),
    )

    chunks = _run(body)

    # The held thought fragment is not merged into the answer, and comes first.
    assert _texts(chunks) == [("think ", True), (TOKEN[:4], True), (f"answer {EMAIL}", False)]


def test_stream_keeps_a_held_back_chunk_that_carries_more_than_text() -> None:
    cited = _chunk({"text": TOKEN[:5]})
    cited["candidates"][0]["citationMetadata"] = {"citations": [{"uri": "https://x.test"}]}
    blocked = {**_chunk({"text": TOKEN[:5]}), "promptFeedback": {"blockReason": "OTHER"}}

    for first in (cited, blocked):
        chunks = _run(_sse(first, _chunk({"text": TOKEN[5:]}, finish="STOP")))

        assert len(chunks) == 2
        assert chunks[0]["candidates"][0]["content"]["parts"] == []
        assert {k: v for k, v in chunks[0].items() if k != "candidates"} == {
            k: v for k, v in first.items() if k != "candidates"
        }
        assert _texts(chunks) == [(EMAIL, False)]
    assert _run(_sse(cited, _chunk(finish="STOP")))[0]["candidates"][0]["citationMetadata"]


def test_stream_keeps_a_held_back_part_that_carries_a_signature() -> None:
    body = _sse(
        _chunk({"text": TOKEN[:5], "thoughtSignature": "sig"}),
        _chunk({"text": TOKEN[5:]}, finish="STOP"),
    )

    chunks = _run(body)

    assert chunks[0]["candidates"][0]["content"]["parts"] == [
        {"text": "", "thoughtSignature": "sig"}
    ]
    assert _texts(chunks) == [("", False), (EMAIL, False)]


def test_stream_cut_off_without_finish_still_ends_with_the_held_text() -> None:
    chunks = _run(_sse(_chunk({"text": "mail " + TOKEN[:7]})))

    assert _texts(chunks) == [("mail ", False), (TOKEN[:7], False)]
    assert chunks[-1] == {
        "candidates": [{"content": {"role": "model", "parts": [{"text": TOKEN[:7]}]}, "index": 0}],
        "modelVersion": "gemini-2.5-flash",
    }


def test_stream_error_event_comes_after_the_held_text() -> None:
    error = {"error": {"code": 500, "message": f"failed for {TOKEN}"}}
    chunks = _run(_sse(_chunk({"text": "a " + TOKEN[:3]}), error))

    assert _texts(chunks) == [("a ", False), (TOKEN[:3], False)]
    assert chunks[-1] == {"error": {"code": 500, "message": f"failed for {EMAIL}"}}


def test_stream_candidates_are_restored_apart() -> None:
    token2 = "<PRIVYX_EMAIL_2>"
    body = _sse(
        {
            "candidates": [
                {"content": {"parts": [{"text": TOKEN[:6]}]}, "index": 0},
                {"content": {"parts": [{"text": token2[:6]}]}, "index": 1},
            ]
        },
        {
            "candidates": [
                {"content": {"parts": [{"text": TOKEN[6:]}]}, "index": 0, "finishReason": "STOP"},
                {"content": {"parts": [{"text": token2[6:]}]}, "index": 1, "finishReason": "STOP"},
            ]
        },
    )

    chunks = _run(body, {TOKEN: EMAIL, token2: "bob@example.org"})

    by_index = {c["index"]: c["content"]["parts"][0]["text"] for c in chunks[-1]["candidates"]}
    assert by_index == {0: EMAIL, 1: "bob@example.org"}


def test_json_array_stream_is_restored_in_order() -> None:
    chunks = [
        _chunk({"text": "Hi " + TOKEN[:9]}),
        _chunk({"text": TOKEN[9:]}, finish="STOP"),
    ]
    mapping = {TOKEN: EMAIL}

    restored, count = restore_chunks(chunks, PseudonymOperator(), FormatCodec.default(), mapping)

    assert "".join(t for t, _ in _texts(restored)) == f"Hi {EMAIL}"
    assert count == 1


def test_usage_metadata_is_read() -> None:
    meter = UsageMeter()
    chunk = {
        "candidates": [],
        "modelVersion": "gemini-2.5-flash",
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 40,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 115,
        },
    }

    meter.feed_sse(f"data: {json.dumps(chunk)}\r\n\r\n")
    meter.close()

    assert meter.model == "gemini-2.5-flash"
    assert meter.usage == {"input": 60, "output": 15, "cache_read": 40, "cache_write": 0}
    array = UsageMeter()
    array.feed_json([{"candidates": []}, chunk])
    assert array.usage == meter.usage


# -- directive -------------------------------------------------------------


async def _directed(payload: dict[str, Any], path: str) -> dict[str, Any]:
    engine = _engine()
    engine.directive = True
    sid = (await engine.get_or_create_session()).session_id
    return await transform_request(payload, engine, sid, schema="gemini", path=path)


async def test_directive_goes_last_in_the_system_instruction() -> None:
    from privyx.proxy.schemas import directive

    text = directive(FormatCodec.default())
    contents = [{"role": "user", "parts": [{"text": "hi"}]}]
    generate = "/v1beta/models/m:generateContent"

    added = await _directed({"contents": contents}, generate)
    after = await _directed(
        {"contents": contents, "system_instruction": {"parts": [{"text": "be brief"}]}}, generate
    )
    wrapped = await _directed(
        {"generateContentRequest": {"model": "models/m", "contents": contents}},
        "/v1beta/models/m:countTokens",
    )

    assert added["systemInstruction"] == {"parts": [{"text": text}]}
    assert after["system_instruction"] == {"parts": [{"text": "be brief"}, {"text": text}]}
    assert wrapped["generateContentRequest"]["systemInstruction"] == {"parts": [{"text": text}]}


async def test_directive_skips_what_gemini_would_refuse() -> None:
    contents = [{"role": "user", "parts": [{"text": "hi"}]}]
    for payload, path in (
        ({"contents": contents}, "/v1beta/models/m:countTokens"),  # takes contents only
        (
            {"contents": contents, "cachedContent": "cachedContents/c"},
            "/v1/models/m:generateContent",
        ),
        ({"content": {"parts": [{"text": "hi"}]}}, "/v1beta/models/e:embedContent"),
    ):
        assert await _directed(payload, path) == payload
