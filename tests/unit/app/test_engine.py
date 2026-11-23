"""``app.engine()``: one engine for the contexts open at once, closed by the last."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import yaml

import privyx.app._build as build
from privyx import Privyx
from privyx.vaults._sqlite import SQLiteVault

REDIS = os.environ.get("PRIVYX_TEST_REDIS_URL", "")


def _app(tmp_path: Path, vault: dict[str, Any] | None = None) -> Privyx:
    path = tmp_path / "config.yaml"
    vault = vault or {"type": "sqlite", "dsn": f"sqlite+aiosqlite:///{tmp_path / 'v.db'}"}
    path.write_text(yaml.safe_dump({"vault": vault}), encoding="utf-8")
    return Privyx(config=path)


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One item per engine built."""
    original, built = build.build_engine, []

    async def counted(*args: Any, **kwargs: Any) -> Any:
        built.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(build, "build_engine", counted)
    return built


async def test_a_task_keeps_the_engine_after_the_one_that_built_it_leaves(tmp_path: Path) -> None:
    app = _app(tmp_path)
    first_in, second_in = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with app.engine():
            first_in.set()
            await second_in.wait()

    task = asyncio.create_task(first())
    await first_in.wait()
    async with app.engine() as engine:
        second_in.set()
        await task
        masked = await engine.transform("write to ana@example.com")

    assert "ana@example.com" not in masked.text
    assert app._engine is None


async def test_tasks_entering_at_once_build_one_engine(tmp_path: Path, builds: list[int]) -> None:
    app = _app(tmp_path)
    seen = []

    async def one() -> None:
        async with app.engine() as engine:
            seen.append(engine)
            await asyncio.sleep(0.01)

    await asyncio.gather(one(), one(), one())

    assert len(builds) == 1
    assert seen[0] is seen[1] is seen[2]


async def test_a_nested_context_leaves_the_engine_to_the_outer_one(
    tmp_path: Path, builds: list[int]
) -> None:
    app = _app(tmp_path)

    async with app.engine() as outer:
        async with app.engine() as inner:
            assert inner is outer
        await outer.transform("write to ana@example.com")  # still open
        await app.masking.mask("write to bob@example.com")  # a service joins it

    assert len(builds) == 1
    assert app._engine is None


@pytest.mark.parametrize(
    "kind",
    [
        "sqlite",
        pytest.param(
            "redis", marks=pytest.mark.skipif(not REDIS, reason="PRIVYX_TEST_REDIS_URL is not set")
        ),
    ],
)
async def test_forty_parallel_masks_all_succeed(tmp_path: Path, kind: str) -> None:
    vault = {"type": "redis", "redis_url": REDIS} if kind == "redis" else None
    app = _app(tmp_path, vault)

    async def one(i: int) -> Any:
        await asyncio.sleep(i * 0.003)  # enter and leave overlapping, not all at once
        return await app.masking.mask(f"write to user{i}@example.com")

    results = await asyncio.gather(*(one(i) for i in range(40)), return_exceptions=True)

    assert [r for r in results if isinstance(r, BaseException)] == []


async def test_a_failed_build_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    original = build.build_engine

    async def fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no vault")

    monkeypatch.setattr(build, "build_engine", fails)
    with pytest.raises(RuntimeError, match="no vault"):
        async with app.engine():
            pass
    assert app._engine_users == 0

    monkeypatch.setattr(build, "build_engine", original)
    async with app.engine() as engine:
        await engine.transform("write to ana@example.com")
    assert app._engine_users == 0


async def test_a_task_cancelled_while_building_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    original = build.build_engine
    building = asyncio.Event()

    async def hangs(*args: Any, **kwargs: Any) -> Any:
        building.set()
        await asyncio.Event().wait()

    async def enters() -> None:
        async with app.engine():
            pass

    monkeypatch.setattr(build, "build_engine", hangs)
    task = asyncio.create_task(enters())
    await building.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app._engine_users == 0

    monkeypatch.setattr(build, "build_engine", original)
    async with app.engine() as engine:
        await engine.transform("write to ana@example.com")


async def test_a_task_cancelled_while_the_vault_opens_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    before = set(threading.enumerate())
    preparing = asyncio.Event()
    connections: list[aiosqlite.Connection] = []  # so that only a close can end its thread

    async def hangs(self: aiosqlite.Connection, *args: Any) -> None:
        connections.append(self)
        preparing.set()
        await asyncio.Event().wait()

    async def enters() -> None:
        async with app.engine():
            pass

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "executescript", hangs)
        task = asyncio.create_task(enters())
        await preparing.wait()
        assert set(threading.enumerate()) - before  # the connection's thread runs
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        for _ in range(100):
            if not set(threading.enumerate()) - before:
                break
            await asyncio.sleep(0.01)
        assert not set(threading.enumerate()) - before
        assert app._engine_users == 0
    finally:
        await connections[0].close()  # a thread left running would hang the test run


async def test_a_task_cancelled_while_the_engine_closes_still_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    original = SQLiteVault.close
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(self: SQLiteVault) -> None:
        closing.set()
        await release.wait()
        await original(self)
        closed.set()

    monkeypatch.setattr(SQLiteVault, "close", slow)

    async def enters() -> None:
        async with app.engine():
            pass

    task = asyncio.create_task(enters())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await asyncio.wait_for(closed.wait(), 2)


def test_one_app_serves_several_event_loops(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def twice() -> None:
        await asyncio.gather(
            app.masking.mask("write to ana@example.com"),
            app.masking.mask("write to bob@example.com"),
        )

    asyncio.run(twice())
    asyncio.run(twice())

    assert app._engine is None
