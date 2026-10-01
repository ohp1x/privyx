"""Unit tests for wire-schema detection and structural transforms."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.session import Session
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy import schemas
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


class _CopyingVault(MemoryVault):
    """Hands out copies and yields on every call, like sqlite/redis."""

    def __init__(self) -> None:
        super().__init__()
        self.gets = self.saves = 0

    async def get(self, session_id: str) -> Session | None:
        self.gets += 1
        await asyncio.sleep(0)
        session = await super().get(session_id)
        return Session.from_dict(session.to_dict()) if session else None

    async def save(self, session: Session) -> None:
        self.saves += 1
        await asyncio.sleep(0)
        await super().save(Session.from_dict(session.to_dict()))


async def test_transform_request_one_vault_read_and_write_per_request() -> None:
    vault = _CopyingVault()
    engine = PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )
    sid = await _session(engine)
    payload = {"messages": [{"role": "user", "content": f"mail u{i}@x.test"} for i in range(50)]}
    vault.gets = vault.saves = 0

    out = await transform_request(payload, engine, sid)

    assert (vault.gets, vault.saves) == (1, 1)
    session = await vault.get(sid)
    assert session is not None and len(session.mapping) == 50
    assert await restore_response(out, engine, sid) == payload


async def test_concurrent_requests_on_one_session_keep_both_mappings() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=_CopyingVault(),
    )
    sid = await _session(engine)

    a, b = await asyncio.gather(
        transform_request(_mail_request("a@x.test"), engine, sid),
        transform_request(_mail_request("b@x.test"), engine, sid),
    )

    # Without the session lock both mint the same counter token and the
    # second save drops the first request's mapping.
    assert a != b
    assert (await restore_response(a, engine, sid)) == _mail_request("a@x.test")
    assert (await restore_response(b, engine, sid)) == _mail_request("b@x.test")


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


async def test_transform_request_covers_echoed_and_tool_fields() -> None:
    """Assistant turns echoed back, and tool results, never reach upstream raw."""
    engine = _engine()
    sid = await _session(engine)
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": f"user is {EMAIL}", "signature": "s"},
                    {"type": "tool_use", "id": "t1", "name": "send", "input": {"to": [EMAIL]}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": f"sent {EMAIL}"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [{"type": "text", "text": f"file: {EMAIL}"}],
                    },
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": f"user is {EMAIL}",
                "tool_calls": [
                    {"function": {"name": "a", "arguments": json.dumps({"to": f"x\n{EMAIL}"})}},
                    {"function": {"name": "b", "arguments": f"{{broken {EMAIL}"}},
                ],
            },
        ]
    }

    out = await transform_request(payload, engine, sid)

    assert EMAIL not in json.dumps(out)
    args = out["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args)["to"].startswith("x\n")  # still valid JSON
    assert out["messages"][0]["content"][0]["signature"] == "s"


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


async def test_restore_anthropic_text_thinking_and_tool_input() -> None:
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
            {"type": "thinking", "thinking": f"mail {pseudonym}", "signature": "sig"},
        ]
    }
    out = await restore_response(response, engine, sid)

    assert out["content"][0]["text"] == f"hi {EMAIL}"
    assert out["content"][1]["input"]["to"] == EMAIL
    assert out["content"][2]["thinking"] == f"mail {EMAIL}"


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


# --------------------------------------------------------------------------
# Generic walk: every content leaf in, every leaf out, opaque keys untouched

ARGS = json.dumps({"to": f"x\n{EMAIL}", "name": "Alice"})  # escaped \n right before


def _args_ok(arguments: str) -> None:
    """Still valid JSON, the escape intact, and the address pseudonymized."""
    parsed = json.loads(arguments)
    assert parsed["to"].startswith("x\n<PRIVYX_EMAIL_")


async def _pseudonym(engine: PrivacyEngine, sid: str) -> str:
    await transform_request(_mail_request(), engine, sid)
    session = await engine.vault.get(sid)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)
    assert pseudonym is not None
    return pseudonym


async def test_transform_anthropic_blocks_fail_closed() -> None:
    engine = _engine()
    sid = await _session(engine)
    pdf = {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"}
    tools = [{"name": "send", "description": "d", "input_schema": {"type": "object"}}]
    payload = {
        "model": "claude-x",
        "tools": tools,
        "system": [{"type": "text", "text": f"op {EMAIL}", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "text", "media_type": "text/plain", "data": EMAIL},
                        "title": f"notes of {EMAIL}",
                        "context": f"from {EMAIL}",
                    },
                    {"type": "document", "source": {"type": "content", "content": EMAIL}},
                    {"type": "document", "source": pdf},
                    {
                        "type": "search_result",
                        "source": "https://acme.test/kb",
                        "title": f"about {EMAIL}",
                        "content": [{"type": "text", "text": f"reach {EMAIL}"}],
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": f"is {EMAIL}", "signature": "sig=="},
                    {"type": "redacted_thinking", "data": "opaque=="},
                    {
                        "type": "text",
                        "text": "see doc",
                        "citations": [
                            {"type": "char_location", "cited_text": EMAIL, "document_index": 0}
                        ],
                    },
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_1",
                        "name": "web_search",
                        "input": {"query": EMAIL, "name": "Alice", "type": EMAIL},
                    },
                    {
                        "type": "bash_code_execution_tool_result",
                        "tool_use_id": "srvtoolu_2",
                        "content": {
                            "type": "bash_code_execution_result",
                            "stdout": EMAIL,
                            "stderr": f"no {EMAIL}",
                            "return_code": 0,
                            "content": [],
                        },
                    },
                ],
            },
        ],
    }
    original = json.dumps(payload)

    out = await transform_request(payload, engine, sid)

    assert EMAIL not in json.dumps(out)
    assert json.dumps(payload) == original  # caller payload untouched
    blocks = out["messages"][1]["content"]
    assert out["model"] == "claude-x" and out["tools"] == tools
    assert out["messages"][0]["content"][2]["source"] == pdf  # base64 data byte-identical
    assert blocks[0]["signature"] == "sig==" and blocks[1]["data"] == "opaque=="
    assert blocks[3]["id"] == "srvtoolu_1" and blocks[3]["name"] == "web_search"
    # Inside a tool input nothing is structural: even "type" is user data.
    assert "<PRIVYX_EMAIL_" in blocks[3]["input"]["type"]
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_transform_tool_descriptions_only() -> None:
    engine = _engine()
    sid = await _session(engine)
    # A property *named* "description", with a PII-looking enum it must keep.
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": f"cc {EMAIL}", "enum": [EMAIL]}
        },
        "required": ["description"],
    }
    payload = {
        "tools": [
            {"name": "send", "description": f"mail {EMAIL}", "input_schema": schema},
            {
                "type": "function",
                "function": {"name": "send", "description": f"mail {EMAIL}", "parameters": schema},
            },
        ],
        "messages": [],
    }
    original = json.dumps(payload)

    out = await transform_request(payload, engine, sid)

    assert json.dumps(payload) == original
    anthropic, openai = out["tools"][0], out["tools"][1]["function"]
    for tool, params in ((anthropic, anthropic["input_schema"]), (openai, openai["parameters"])):
        assert tool["name"] == "send"
        assert tool["description"].startswith("mail <PRIVYX_EMAIL_")
        prop = params["properties"]["description"]
        assert prop["description"].startswith("cc <PRIVYX_EMAIL_")
        assert prop["enum"] == [EMAIL] and params["required"] == ["description"]


async def test_transform_numbers_the_cache_prefix_first() -> None:
    # Ephemeral sessions: a fresh session per request.  A value first seen in a
    # later message must not renumber the system prompt (a cache miss per turn).
    system = f"owner {PHONE}"
    turn1 = {"messages": [{"role": "user", "content": f"mail {EMAIL}"}], "system": system}
    turn2 = {
        "messages": [
            *turn1["messages"],
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "cc bob@example.com"},
        ],
        "system": system,
    }
    outs = []
    for payload in (turn1, turn2):
        engine = _engine()
        outs.append(await transform_request(payload, engine, await _session(engine)))

    assert outs[0]["system"] == outs[1]["system"]
    assert list(outs[1]) == ["messages", "system"]  # key order kept


async def test_transform_masks_what_the_client_reports_about_its_machine() -> None:
    engine = _engine()
    sid = await _session(engine)
    # Claude Code's `safeguards`.  In a message `status`, `name`, and `url` are
    # wire structure and skipped; here they hold file names and git remotes.
    context = {
        "permission_mode": "auto",
        "live_cwd": f"/home/{EMAIL}/app",
        "git_state": {
            "status": {"clean": False, "porcelain": f"?? {EMAIL}.txt"},
            "remotes": [{"name": EMAIL, "url": f"https://git.example/{EMAIL}/app.git"}],
        },
    }
    payload = {
        "safeguards": [{"type": "dangerous_tool_use", "classifier_context": context}],
        "messages": [],
    }
    original = json.dumps(payload)

    out = await transform_request(payload, engine, sid)

    assert EMAIL not in json.dumps(out)
    assert json.dumps(payload) == original  # caller payload untouched
    masked = out["safeguards"][0]["classifier_context"]
    assert out["safeguards"][0]["type"] == "dangerous_tool_use"
    assert masked["permission_mode"] == "auto" and masked["git_state"]["status"]["clean"] is False
    assert masked["live_cwd"].startswith("/home/<PRIVYX_EMAIL_")


async def test_machine_metadata_does_not_renumber_the_conversation() -> None:
    # `safeguards` comes with a session's first request only, and ahead of the
    # messages.  Numbered first, its values would shift every token after them
    # on that one turn: a prompt-cache miss, and an echoed turn that differs.
    conversation = {
        "system": f"owner {PHONE}",
        "messages": [{"role": "user", "content": f"mail {EMAIL}"}],
    }
    context = {"home_dir": "/home/bob@example.com", "live_cwd": f"/srv/{EMAIL}"}
    first = {"safeguards": [{"classifier_context": context}], **conversation}
    outs = []
    for payload in (first, conversation):
        engine = _engine()
        outs.append(await transform_request(payload, engine, await _session(engine)))

    assert outs[0]["system"] == outs[1]["system"]
    assert outs[0]["messages"] == outs[1]["messages"]
    # A value in both places gets one token.
    token = outs[0]["messages"][0]["content"].removeprefix("mail ")
    assert outs[0]["safeguards"][0]["classifier_context"]["live_cwd"] == f"/srv/{token}"
    assert list(outs[0]) == ["safeguards", "system", "messages"]  # key order kept


def _codex_metadata(path: str) -> str:
    """Codex's turn metadata: a JSON string with the workspace path as a key."""
    remotes = {"origin": f"https://git.example{path}.git"}
    workspaces = {path: {"associated_remote_urls": remotes, "has_changes": False}}
    return json.dumps({"session_id": "s-1", "workspaces": workspaces})


async def test_transform_masks_codex_turn_metadata_in_body_and_header() -> None:
    vault = _CopyingVault()
    engine = PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )
    sid = await _session(engine)
    metadata = _codex_metadata(f"/home/{EMAIL}/app")
    payload = {
        "client_metadata": {"session_id": "s-1", "x-codex-turn-metadata": metadata},
        "input": f"mail {EMAIL}",
    }
    headers = {"x-codex-turn-metadata": metadata, "authorization": "Bearer key"}
    vault.gets = vault.saves = 0

    out = await transform_request(payload, engine, sid, headers=headers)

    assert (vault.gets, vault.saves) == (1, 1)  # one session scope for body and header
    token = out["input"].removeprefix("mail ")
    assert token.startswith("<PRIVYX_EMAIL_")
    body_copy = out["client_metadata"]["x-codex-turn-metadata"]
    assert headers == {"x-codex-turn-metadata": body_copy, "authorization": "Bearer key"}
    assert EMAIL not in body_copy
    assert json.loads(body_copy) == json.loads(_codex_metadata(f"/home/{token}/app"))


async def test_metadata_json_escapes_are_decoded_before_masking() -> None:
    # Codex writes /home/josé as /home/josé, which no detector would match.
    engine = PrivacyEngine(
        detector=RegexDetector({"PERSON": "josé"}),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    sid = await _session(engine)
    metadata = _codex_metadata("/home/josé/zoë")
    assert metadata.isascii() and "jos\\u00e9" in metadata
    headers = {"x-codex-turn-metadata": metadata}
    payload = {"client_metadata": {"x-codex-turn-metadata": metadata}}

    out = await transform_request(payload, engine, sid, headers=headers)

    header = headers["x-codex-turn-metadata"]
    for masked in (header, out["client_metadata"]["x-codex-turn-metadata"]):
        (path,) = json.loads(masked)["workspaces"]
        assert path.startswith("/home/<PRIVYX_PERSON_") and path.endswith("/zoë")
    assert header.isascii()  # a header value is one line of ASCII


async def test_echoed_thinking_gets_the_signed_text_back() -> None:
    engine = _engine()
    sid = await _session(engine)
    request = await transform_request(_mail_request(), engine, sid)
    token = request["messages"][0]["content"].removeprefix("mail ")
    signed = f"mail {token}; vendor is ops@vendor.test"  # the model's own email
    response = {"content": [{"type": "thinking", "thinking": signed, "signature": "sig-b-1"}]}

    restored = await restore_response(response, engine, sid)
    block = restored["content"][0]
    assert block["thinking"] == f"mail {EMAIL}; vendor is ops@vendor.test"

    echo = {"messages": [*_mail_request()["messages"], {"role": "assistant", "content": [block]}]}
    out = await transform_request(echo, engine, sid)

    assert out["messages"][1]["content"][0]["thinking"] == signed
    assert echo["messages"][1]["content"][0]["thinking"] == block["thinking"]  # no mutation


def test_thinking_store_is_bounded_by_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schemas, "_THINKING", {})
    monkeypatch.setattr(schemas, "_THINKING_MAX_CHARS", 1000)
    for i in range(10):
        schemas.remember_thinking(f"sig-{i}", "t" * 300)  # 305 characters each

    assert list(schemas._THINKING) == ["sig-7", "sig-8", "sig-9"]  # the oldest go first


async def test_transform_openai_chat_gaps() -> None:
    engine = _engine()
    sid = await _session(engine)
    payload = {
        "model": "gpt-x",
        "response_format": {"type": "json_schema", "json_schema": {"name": "reply"}},
        "prediction": {"type": "content", "content": f"draft {EMAIL}"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"mail {EMAIL}"},
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                    {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": f"not {EMAIL}"}],
                "refusal": f"no {EMAIL}",
                "reasoning": f"user is {EMAIL}",
                "function_call": {"name": "send", "arguments": ARGS},
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "send", "arguments": ARGS},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": f"sent {EMAIL}"},
        ],
    }

    out = await transform_request(payload, engine, sid)

    assert EMAIL not in json.dumps(out)
    assistant = out["messages"][1]
    _args_ok(assistant["function_call"]["arguments"])
    _args_ok(assistant["tool_calls"][0]["function"]["arguments"])
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["function_call"]["name"] == "send"
    assert out["messages"][0]["content"][1]["image_url"] == {"url": "https://x.test/a.png"}
    assert out["messages"][0]["content"][2]["input_audio"]["data"] == "UklGRg=="
    assert out["messages"][2]["tool_call_id"] == "call_1"
    assert out["response_format"] == payload["response_format"]


def _responses_request() -> dict[str, Any]:
    return {
        "model": "gpt-x",
        "instructions": f"assist {EMAIL}",
        "text": {"format": {"type": "text"}},
        "reasoning": {"effort": "high", "summary": "auto"},
        "tools": [{"type": "function", "name": "send", "parameters": {"type": "object"}}],
        "prompt": {"id": "pmpt_1", "variables": {"who": EMAIL}},
        "input": [
            {"type": "message", "role": "developer", "content": f"ctx {EMAIL}"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"mail {EMAIL}"},
                    {"type": "input_image", "image_url": "data:image/png;base64,iVBOR"},
                    {"type": "input_file", "file_data": "JVBERi0=", "filename": "a.pdf"},
                ],
            },
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "gAAAA==",
                "summary": [{"type": "summary_text", "text": f"think {EMAIL}"}],
                "content": [{"type": "reasoning_text", "text": f"raw {EMAIL}"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_1",
                "status": "completed",
                "content": [{"type": "output_text", "text": f"ok {EMAIL}", "annotations": []}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "send",
                "arguments": ARGS,
            },
            {"type": "function_call_output", "call_id": "call_1", "output": f"sent {EMAIL}"},
            {
                "type": "custom_tool_call",
                "call_id": "call_2",
                "name": "apply_patch",
                "input": f"*** {EMAIL}",
            },
            {"type": "custom_tool_call_output", "call_id": "call_2", "output": f"done {EMAIL}"},
            {
                "type": "local_shell_call",
                "id": "lsh_1",
                "call_id": "call_3",
                "status": "completed",
                "action": {"type": "exec", "command": ["grep", EMAIL], "env": {}},
            },
            {"type": "local_shell_call_output", "id": "call_3", "output": f"{EMAIL}\n"},
            {
                "type": "shell_call_output",
                "call_id": "call_4",
                "output": [
                    {
                        "stdout": EMAIL,
                        "stderr": f"warn {EMAIL}",
                        "outcome": {"type": "exit", "exit_code": 0},
                    }
                ],
            },
            {
                "type": "apply_patch_call",
                "call_id": "call_5",
                "status": "completed",
                "operation": {"type": "update_file", "path": "a.txt", "diff": f"+{EMAIL}"},
            },
            {
                "type": "mcp_call",
                "id": "mcp_1",
                "name": "lookup",
                "server_label": "crm",
                "arguments": ARGS,
                "output": f"found {EMAIL}",
            },
        ],
    }


async def test_transform_responses_request() -> None:
    engine = _engine()
    sid = await _session(engine)
    payload = _responses_request()

    out = await transform_request(payload, engine, sid)

    assert EMAIL not in json.dumps(out)
    for key in ("model", "text", "reasoning", "tools"):
        assert out[key] == payload[key]  # config, left alone
    items = out["input"]
    assert out["prompt"]["id"] == "pmpt_1"
    assert items[1]["content"][1]["image_url"] == "data:image/png;base64,iVBOR"
    assert items[1]["content"][2]["file_data"] == "JVBERi0="
    assert items[2]["encrypted_content"] == "gAAAA==" and items[2]["id"] == "rs_1"
    assert items[4]["name"] == "send" and items[4]["call_id"] == "call_1"
    _args_ok(items[4]["arguments"])
    _args_ok(items[-1]["arguments"])  # mcp_call

    # The string shorthand is the user message itself.
    shorthand = await transform_request({"input": f"mail {EMAIL}"}, engine, sid)
    assert shorthand["input"].startswith("mail <PRIVYX_EMAIL_")


async def test_restore_walks_every_leaf_of_all_three_shapes() -> None:
    engine = _engine()
    sid = await _session(engine)
    token = await _pseudonym(engine, sid)
    args = json.dumps({"to": f"x\n{token}"})

    anthropic = {
        "id": "msg_1",
        "content": [
            {"type": "text", "text": "cf", "citations": [{"cited_text": token}]},
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
                "input": {"query": token},
            },
            {
                "type": "code_execution_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {"type": "code_execution_result", "stdout": token, "stderr": token},
            },
            {"type": "thinking", "thinking": token, "signature": token},  # opaque stays
        ],
    }
    chat = {
        "choices": [
            {
                "message": {
                    "refusal": token,
                    "reasoning": token,
                    "function_call": {"name": "send", "arguments": args},
                }
            }
        ],
    }
    responses = {
        "id": "resp_1",
        "object": "response",
        "instructions": f"assist {token}",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": token}],
            },
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": f"hi {token}", "annotations": []},
                    {"type": "refusal", "refusal": token},
                ],
            },
            {"type": "function_call", "call_id": "call_1", "name": "send", "arguments": args},
            {
                "type": "mcp_call",
                "id": "mcp_1",
                "name": "m",
                "server_label": "crm",
                "arguments": args,
                "output": token,
            },
        ],
    }

    a = await restore_response(anthropic, engine, sid)
    c = await restore_response(chat, engine, sid)
    r = await restore_response(responses, engine, sid)

    assert a["content"][0]["citations"][0]["cited_text"] == EMAIL
    assert a["content"][1]["input"]["query"] == EMAIL
    assert a["content"][2]["content"]["stdout"] == EMAIL
    assert a["content"][3]["signature"] == token
    message = c["choices"][0]["message"]
    assert message["refusal"] == EMAIL and message["reasoning"] == EMAIL
    assert json.loads(message["function_call"]["arguments"])["to"] == f"x\n{EMAIL}"
    assert token not in json.dumps(r)
    assert r["instructions"] == f"assist {EMAIL}"
    assert json.loads(r["output"][2]["arguments"])["to"] == f"x\n{EMAIL}"
