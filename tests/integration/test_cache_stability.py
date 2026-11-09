"""Turns a client sends again reach the upstream byte for byte as before.

Prompt caching matches the request prefix exactly, so masking must give an
echoed turn the same bytes on every later turn: the same tokens for the same
values, also for a value that first shows up later (in a new message, or in
the model's own reply), with nothing renumbered.  That holds for a session
kept across turns, and also for the default fresh session per request, where
it rests on values being numbered in the order they first appear.  The client here behaves like
a coding agent: it keeps the history, appends the restored reply and a new
user message, and sends it all again.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from privyx.core.engine import PrivacyEngine
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

USERS = [
    "mail alice@example.com about the invoice",
    "and copy bob@example.org, from 10.0.0.7",
    "send the summary to alice@example.com and dave@example.com",
]


def _reply(sent: dict[str, Any], schema: str) -> dict[str, Any]:
    """The model repeats the last user message and adds a value it made up."""
    last = sent["messages"][-1]["content"]
    text = f"Done: {last}. Also told carol@example.net."
    if schema == "anthropic":
        return {"content": [{"type": "text", "text": text}]}
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _assistant(body: dict[str, Any], schema: str) -> dict[str, Any]:
    if schema == "anthropic":
        return {"role": "assistant", "content": body["content"]}
    return body["choices"][0]["message"]


@pytest.mark.parametrize("schema", ["openai", "anthropic"])
@pytest.mark.parametrize("strategy", ["ephemeral", "header", "conversation"])
@pytest.mark.parametrize("anchored", [False, True])
@pytest.mark.parametrize("directive", [False, True])
async def test_echoed_turns_are_byte_identical(
    schema: str, strategy: str, anchored: bool, directive: bool
) -> None:
    sent: list[bytes] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json=_reply(json.loads(request.content), schema))

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(anchor=HMACAnchor("s" * 32) if anchored else None),
        vault=MemoryVault(),
        directive=directive,
    )
    proxy = TransparentProxy(
        engine,
        origin="https://up.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        session_strategy="conversation" if strategy == "conversation" else "ephemeral",
    )
    headers = {"authorization": "Bearer k"}
    if strategy == "header":
        headers["x-privyx-session"] = "ses_cache"
    path = "v1/messages" if schema == "anthropic" else "v1/chat/completions"
    system = "You help ops@example.com's team."
    request: dict[str, Any] = {
        "model": "m",
        "tools": [{"name": "mail", "description": "Mails root@example.com", "parameters": {}}],
        **({"system": system} if schema == "anthropic" else {}),
        "messages": [] if schema == "anthropic" else [{"role": "system", "content": system}],
    }

    for text in USERS:
        request["messages"].append({"role": "user", "content": text})
        result = await proxy.handle(
            method="POST", path=path, headers=headers, body=json.dumps(request).encode()
        )
        assert result.status_code == 200
        request["messages"].append(_assistant(json.loads(result.body), schema))

    for before, after in zip(sent, sent[1:], strict=False):
        # Everything up to the last message the earlier turn sent is a prefix.
        assert after.startswith(before[: -len(b"]}")]), (before, after)
    assert b"alice@example.com" not in b"".join(sent)
    assert b"carol@example.net" not in b"".join(sent)
