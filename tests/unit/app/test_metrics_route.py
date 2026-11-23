"""``GET /metrics`` serves the metrics feature running now."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml

from privyx import Privyx
from privyx.events import ProxyBlocked


def _app(tmp_path: Path, **features: object) -> Privyx:
    path = tmp_path / "config.yaml"
    data = {"features": {"audit": {"enabled": False}, **features}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Privyx(config=path)


@asynccontextmanager
async def _served(app: Privyx) -> AsyncIterator[httpx.AsyncClient]:
    stack = app.asgi()
    async with stack.router.lifespan_context(stack):
        transport = httpx.ASGITransport(app=stack)
        async with httpx.AsyncClient(transport=transport, base_url="http://privyx.test") as client:
            yield client


def _blocked(app: Privyx) -> None:
    app._events.notify(
        ProxyBlocked(timestamp=time.time(), fields={"path": "/v1/x", "reason": "test"})
    )


async def test_metrics_stop_when_the_feature_is_disabled(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with _served(app) as client:
        assert (await client.get("/metrics")).status_code == 200
        await app.features.disable("metrics")
        assert (await client.get("/metrics")).status_code == 404


async def test_metrics_start_when_the_feature_is_enabled(tmp_path: Path) -> None:
    app = _app(tmp_path, metrics={"enabled": False})
    async with _served(app) as client:
        assert (await client.get("/metrics")).status_code == 404
        await app.features.enable("metrics")
        assert (await client.get("/metrics")).status_code == 200
        await app.features.disable("metrics")


async def test_metrics_enabled_while_serving_end_with_the_server(tmp_path: Path) -> None:
    app = _app(tmp_path, metrics={"enabled": False})
    stack = app.asgi()

    async def status() -> int:
        transport = httpx.ASGITransport(app=stack)
        async with httpx.AsyncClient(transport=transport, base_url="http://privyx.test") as client:
            return (await client.get("/metrics")).status_code

    async with stack.router.lifespan_context(stack):
        assert await status() == 404
        await app.features.enable("metrics")
        assert await status() == 200

    async with stack.router.lifespan_context(stack):
        assert await status() == 404


async def test_metrics_follow_the_feature_that_replaced_it(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with _served(app) as client:
        await app.features.enable("metrics")
        _blocked(app)
        body = (await client.get("/metrics")).text

    assert 'privyx_audit_events_total{event="proxy.blocked"} 1' in body
