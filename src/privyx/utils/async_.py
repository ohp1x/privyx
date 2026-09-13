"""Async utilities — helpers for async iteration and concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, cast


async def run_sync[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous callable in the default thread pool executor.

    Keeps the event loop unblocked for CPU-bound or blocking I/O calls.

    Example::

        result = await run_sync(some_blocking_function, arg1, arg2)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def collect[T](ait: AsyncIterator[T]) -> list[T]:
    """Drain an async iterator into a list."""
    return [item async for item in ait]


async def first[T](ait: AsyncIterator[T], default: T | None = None) -> T | None:
    """Return the first item from an async iterator, or ``default``."""
    async for item in ait:
        return item
    return default


def ensure_coroutine[T](
    func: Callable[..., T | Coroutine[Any, Any, T]],
) -> Callable[..., Coroutine[Any, Any, T]]:
    """Wrap a sync function so it can be awaited.

    If ``func`` already returns a coroutine, it is returned unchanged.
    Otherwise it is wrapped in :func:`run_sync`.
    """
    import inspect

    if inspect.iscoroutinefunction(func):
        return func

    sync_func = cast(Callable[..., T], func)

    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await run_sync(sync_func, *args, **kwargs)

    return wrapper
