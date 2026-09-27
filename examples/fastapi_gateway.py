"""Run the Privyx gateway on FastAPI programmatically."""

from __future__ import annotations

import asyncio

import uvicorn

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.gateway.server import Gateway
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.providers.registry import build_provider
from privyx.vault.memory import MemoryVault


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )

    settings = Settings()
    provider = build_provider(settings)
    gateway = Gateway(engine=engine, provider=provider, settings=settings)

    config = uvicorn.Config(gateway.app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    print("Gateway running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    try:
        await server.serve()
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())