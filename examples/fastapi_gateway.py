"""Run the Privyx gateway on FastAPI programmatically."""

from __future__ import annotations

import asyncio

import uvicorn

from privyx import Privyx


async def main() -> None:
    # The settings `privyx proxy --gateway -u http://localhost:20128` would use.
    app = Privyx(proxy={"mode": "gateway"}, provider={"base_url": "http://localhost:20128"})

    # Its lifespan opens the engine and the provider as uvicorn starts, and
    # closes them as it stops.
    config = uvicorn.Config(app.asgi(), host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    print("Gateway running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
