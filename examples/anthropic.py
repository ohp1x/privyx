"""Use the Privyx engine with Anthropic-style streaming.

Anthropic emits ``content_block_delta`` SSE events with ``delta.text``.
The adapter ignores ``thinking`` deltas so they pass through untouched.
"""

from __future__ import annotations

import asyncio
import json

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
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
    transformed = await engine.transform("My phone is +1 (555) 123-4567", session=session)
    pseudo = transformed.text.split("My phone is ")[1]

    adapter = AnthropicStreamAdapter()
    deanonymizer = StreamingDeanonymizer(session.mapping)

    # 1. A thinking delta — passed through untouched.
    thinking = SSEEvent(
        data='{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"..."}}'
    )
    print("thinking extract:", repr(adapter.extract_delta(thinking)))

    # 2. Text deltas that split the pseudonym across chunks.
    restored: list[str] = []
    for raw in (pseudo[:7], pseudo[7:16], pseudo[16:]):
        # Wrap each delta in an Anthropic-style text_delta event.
        event = SSEEvent(
            data=json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": raw}}
            )
        )
        delta = adapter.extract_delta(event)
        restored.append(deanonymizer.feed(delta))
    restored.append(deanonymizer.flush())
    print("restored:", "My phone is " + "".join(restored))


if __name__ == "__main__":
    asyncio.run(main())