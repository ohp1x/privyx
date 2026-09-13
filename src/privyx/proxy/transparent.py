"""Transparent proxy — intercepts traffic at the network level.

Placeholder for future man-in-the-middle / SOCKS proxy functionality.
"""

from __future__ import annotations


class TransparentProxy:
    """Transparent MITM proxy (placeholder)."""

    async def start(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        raise NotImplementedError("TransparentProxy is not yet implemented")

    async def stop(self) -> None:
        raise NotImplementedError("TransparentProxy is not yet implemented")