"""Integration tests: end-to-end engine + streaming + proxy (in-process)."""

from __future__ import annotations

import json

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
from privyx.streaming.deanonymizer import StreamingDeanonymizer
from privyx.streaming.sse import SSEEvent
from privyx.vault.memory import MemoryVault


def _make_engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


@pytest.mark.asyncio
async def test_request_pseudonymize_response_deanonymize() -> None:
    engine = _make_engine()
    session = await engine.get_or_create_session()

    request = {"messages": [{"role": "user", "content": "My email is alice@example.com"}]}
    transformed = await engine.transform(request["messages"][0]["content"], session=session)
    request["messages"][0]["content"] = transformed.text

    # Simulate the provider echoing back the pseudonym in an SSE stream.
    pseudo = transformed.text.split("My email is ")[1]
    adapter = OpenAIStreamAdapter()
    deanonymizer = StreamingDeanonymizer(session.mapping)

    chunks = [pseudo[:5], pseudo[5:]]
    out_deltas: list[str] = []
    for chunk in chunks:
        event = SSEEvent(data=json.dumps({"choices": [{"delta": {"content": chunk}}]}))
        delta = adapter.extract_delta(event)
        out_deltas.append(deanonymizer.feed(delta))
    out_deltas.append(deanonymizer.flush())

    restored = "My email is " + "".join(out_deltas)
    assert restored == "My email is alice@example.com"
