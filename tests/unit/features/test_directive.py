"""The ``directive`` and ``cache_control`` features: what they add to a masked request."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from privyx.core import LeakError
from privyx.detectors import RegexDetector
from privyx.dialects import Dialect, _anthropic, builtins
from privyx.engine import PrivacyEngine
from privyx.events import Headers, RequestMasked
from privyx.features import CacheControl, Directive
from privyx.features._directive import directive
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.proxy import transform_request
from privyx.token import FormatCodec
from privyx.vaults import MemoryVault

EMAIL = "alice@example.com"
DIRECTIVE = directive(FormatCodec.default())
BIG = "x" * _anthropic.CACHE_MIN_CHARS
EPHEMERAL = {"type": "ephemeral"}


def _engine(*, dlp_check: bool = False) -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        dlp_check=dlp_check,
    )


def _mail_request() -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}


async def _masked(
    payload: dict[str, Any],
    name: str | None,
    *listeners: Callable[[RequestMasked], None],
    path: str = "",
    engine: PrivacyEngine | None = None,
) -> dict[str, Any]:
    """``payload`` masked as the pipeline masks it, ``listeners`` hearing ``request.masked``."""
    engine = engine or _engine()
    sid = (await engine.get_or_create_session()).session_id
    dialect: Dialect | None = builtins()[name] if name is not None else None

    async def masked(body: dict[str, Any]) -> None:
        assert dialect is not None
        event = RequestMasked("POST", path, Headers({}), body, dialect, sid, "req")
        for listener in listeners:
            listener(event)

    hook: Callable[[dict[str, Any]], Awaitable[None]] | None = masked if dialect else None
    return await transform_request(payload, engine, sid, dialect=dialect, masked=hook)


def test_both_are_off_by_default() -> None:
    assert not Directive.enabled_by_default and not CacheControl.enabled_by_default
    assert "<PRIVYX_EMAIL_1>" in DIRECTIVE


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        (None, DIRECTIVE),
        ("be brief", f"be brief\n\n{DIRECTIVE}"),
        (
            [{"type": "text", "text": "first"}],
            [{"type": "text", "text": "first"}, {"type": "text", "text": DIRECTIVE}],
        ),
    ],
)
async def test_directive_goes_after_the_anthropic_system_prompt(system: Any, expected: Any) -> None:
    payload = {**({"system": system} if system else {}), **_mail_request()}

    out = await _masked(payload, "anthropic", Directive().add)

    assert out["system"] == expected
    assert EMAIL not in json.dumps(out)
    assert "system" not in payload or payload["system"] == system  # not mutated


async def test_directive_goes_after_the_leading_chat_system_messages() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "a"},
            {"role": "developer", "content": "b"},
            {"role": "user", "content": f"mail {EMAIL}"},
            {"role": "system", "content": "late"},
        ]
    }

    out = await _masked(payload, "openai", Directive().add)

    roles = [(m["role"], m["content"]) for m in out["messages"]]
    assert roles[2] == ("system", DIRECTIVE)
    assert [r for r, _ in roles] == ["system", "developer", "system", "user", "system"]
    assert len(payload["messages"]) == 4


async def test_directive_comes_first_in_a_responses_input() -> None:
    out = await _masked({"input": "hi"}, "responses", Directive().add)
    resumed = await _masked(
        {"input": "hi", "previous_response_id": "resp_1", "instructions": "x"},
        "responses",
        Directive().add,
    )

    assert out["input"][0]["role"] == "developer"
    assert out["input"][0]["content"][0]["text"] == DIRECTIVE
    assert out["input"][1] == {"role": "user", "content": "hi"}
    assert resumed["input"] == "hi"
    assert resumed["instructions"] == f"x\n\n{DIRECTIVE}"


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        ("embeddings", {"input": "hi"}),
        ("openai", {"prompt": "hi"}),  # legacy completions
        (None, _mail_request()),  # not on a masking route: no event
    ],
)
async def test_directive_skips_requests_without_a_chat(schema: Any, payload: Any) -> None:
    out = await _masked(payload, schema, Directive().add)

    assert DIRECTIVE not in json.dumps(out)


async def test_what_a_listener_adds_is_checked_for_leaks() -> None:
    def copies(event: RequestMasked) -> None:
        event.body["metadata"] = {"note": f"for {EMAIL}"}

    with pytest.raises(LeakError, match="what a listener added"):
        await _masked(_mail_request(), "openai", copies, engine=_engine(dlp_check=True))


async def test_the_directive_passes_the_leak_check() -> None:
    engine = _engine(dlp_check=True)

    out = await _masked(_mail_request(), "openai", Directive().add, engine=engine)

    assert out["messages"][0] == {"role": "system", "content": DIRECTIVE}


async def test_cache_control_marks_a_large_system_and_tools_prefix() -> None:
    tools = [{"name": "a", "input_schema": {}}, {"name": "b", "description": BIG}]
    payload = {"tools": tools, "system": "be brief", **_mail_request()}

    out = await _masked(payload, "anthropic", CacheControl().mark)

    assert out["tools"][-1]["cache_control"] == EPHEMERAL
    assert "cache_control" not in out["tools"][0]
    assert out["system"] == [{"type": "text", "text": "be brief", "cache_control": EPHEMERAL}]
    assert EMAIL not in json.dumps(out)
    assert "cache_control" not in tools[-1] and payload["system"] == "be brief"  # not mutated


async def test_cache_control_marks_the_prefix_with_the_directive_in_it() -> None:
    payload = {"system": BIG, **_mail_request()}

    out = await _masked(payload, "anthropic", Directive().add, CacheControl().mark)

    assert out["system"] == [
        {"type": "text", "text": f"{BIG}\n\n{DIRECTIVE}", "cache_control": EPHEMERAL}
    ]


@pytest.mark.parametrize(
    ("payload", "schema"),
    [
        ({"system": "short", **_mail_request()}, "anthropic"),  # too small to cache
        ({"system": BIG, **_mail_request()}, "openai"),  # not Anthropic
        (  # the client already caches
            {
                "system": BIG,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "hi", "cache_control": EPHEMERAL}],
                    }
                ],
            },
            "anthropic",
        ),
    ],
)
async def test_cache_control_leaves_a_request_alone(payload: dict[str, Any], schema: str) -> None:
    out = await _masked(payload, schema, CacheControl().mark)

    assert out.get("system") == payload["system"]
