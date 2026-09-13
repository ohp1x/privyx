"""Development helper script — quick smoke tests of the package."""

from __future__ import annotations

import asyncio

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.vault.memory import MemoryVault


async def smoke() -> None:
    print("Privyx dev smoke test")
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    text = "Hi, my email is carol@example.com"
    out = await engine.transform(text, session=session)
    print(f"  transform:  {out.text}")
    back = await engine.restore(out.text, session.session_id)
    assert back.text == text, "roundtrip failed"
    print("  roundtrip:  OK")


if __name__ == "__main__":
    asyncio.run(smoke())