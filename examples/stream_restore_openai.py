"""Restore an OpenAI-style stream by hand, without the proxy or a provider.

Pseudonymizes a request, then feeds a simulated SSE reply through the
streaming deanonymizer, with a token split between two chunks.  The proxy does
all of this for you; this is the building block it uses.
"""

from __future__ import annotations

import asyncio
import json

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.deanonymizer import StreamingDeanonymizer
from privyx.streaming.sse import SSEEvent
from privyx.vault.memory import MemoryVault


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )

    session = await engine.get_or_create_session()
    request_text = "Tell me about Alice at alice@example.com"
    transformed = await engine.transform(request_text, session=session)
    print(f"Request pseudonymized: {transformed.text}")

    # Simulate provider SSE chunks that split a pseudonym at the boundary.
    # Suppose the provider echoes back the pseudonym as a stream:
    pseudo = transformed.text.split("Tell me about ")[1]
    chunk1 = pseudo[:10]
    chunk2 = pseudo[10:]

    adapter = SSEStreamAdapter(data_path=["choices", "0", "delta", "content"])
    deanonymizer = StreamingDeanonymizer(session.mapping)

    outputs: list[str] = []
    for raw in (chunk1, chunk2):
        # Wrap each raw delta in an OpenAI-style SSE event envelope.
        event = SSEEvent(
            data=json.dumps({"choices": [{"delta": {"content": raw}}]}),
            event="chat.completion.chunk",
        )
        delta = adapter.extract_delta(event)
        restored = deanonymizer.feed(delta)
        if restored:
            outputs.append(restored)
    outputs.append(deanonymizer.flush())

    restored_text = "Tell me about " + "".join(outputs)
    print(f"Stream restored: {restored_text}")
    assert restored_text == request_text


if __name__ == "__main__":
    asyncio.run(main())