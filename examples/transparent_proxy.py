"""Minimal transparent proxy loop — forwards raw bytes between client and upstream."""

from __future__ import annotations

import asyncio

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.providers.registry import resolve_base_url
from privyx.vault.memory import MemoryVault

# NOTE: This is a conceptual example.  A production transparent proxy needs
# connection handling, TLS interception, and stream splicing.  The intended
# architecture separates the engine from the proxy; see gateway/server.py.


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    settings = Settings()
    print(f"Transparent proxy concept (upstream: {resolve_base_url(settings)})")
    print("Production implementation is in privyx/proxy/transparent.py")
    await engine.get_or_create_session()


if __name__ == "__main__":
    asyncio.run(main())