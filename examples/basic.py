"""Basic usage of the Privyx privacy engine."""

from __future__ import annotations

import asyncio

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.vault.memory import MemoryVault


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )

    # A session is created and persisted automatically on first transform.
    session = await engine.get_or_create_session()
    text = "Contact John at john@example.com or +1 (555) 123-4567."
    print(f"Original: {text}")
    print(f"Session:  {session.session_id}")

    result = await engine.transform(text, session=session)
    print(f"Pseudonymized: {result.text}")

    # Restore (deanonymize) using the session mapping.
    restored = await engine.restore(result.text, session.session_id)
    print(f"Restored: {restored.text}")
    assert restored.text == text


if __name__ == "__main__":
    asyncio.run(main())