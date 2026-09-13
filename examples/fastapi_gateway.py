"""Run the Privyx gateway on FastAPI programmatically."""

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

    try:
        import uvicorn

        from privyx.config.schema import Settings
        from privyx.gateway.server import Gateway
        from privyx.providers.registry import build_provider
        from privyx.proxy.http import HTTPProxy
    except ImportError:
        print("This example requires privyx[server]. Install with: pip install privyx[server]")
        return

    settings = Settings()
    provider = build_provider(settings)
    proxy = HTTPProxy(engine=engine, provider=provider)
    gateway = Gateway(engine=engine, proxy=proxy, settings=settings)

    config = uvicorn.Config(gateway.app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    print("Gateway running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    try:
        await server.serve()
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())