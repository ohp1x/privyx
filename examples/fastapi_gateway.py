"""Run the Privyx gateway on FastAPI programmatically."""

from __future__ import annotations

import asyncio

import uvicorn

from privyx import PrivacyEngine
from privyx.config.schema import Settings
from privyx.detectors import RegexDetector
from privyx.gateway.server import Gateway
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.providers import GenericProvider
from privyx.vaults import MemoryVault


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )

    settings = Settings()
    provider = GenericProvider(base_url="http://localhost:20128")
    gateway = Gateway(engine=engine, provider=provider, settings=settings)

    config = uvicorn.Config(gateway.app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    print("Gateway running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    try:
        await server.serve()
    finally:
        await gateway.close()
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())