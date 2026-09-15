"""Plugin lifecycle hooks.

A plugin module may define module-level ``on_startup()`` / ``on_shutdown()``
callables (sync or async).  The loader collects them into a :class:`HookManager`
that the CLI fires once the engine is built (startup) and during teardown
(shutdown).

Startup and shutdown intentionally differ in how they treat failure: a startup
hook that raises aborts the whole process (a plugin that cannot initialise must
not serve traffic in a broken state), while a shutdown hook that raises is
logged and swallowed (cleanup is best-effort and must not mask the exit path or
stop the other plugins' teardown).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from privyx.core.errors import ConfigError

logger = logging.getLogger("privyx.plugins")

#: A lifecycle hook: a no-arg callable that may be sync or return an awaitable.
Hook = Callable[[], Any | Awaitable[Any]]


class HookManager:
    """Collects and fires plugin lifecycle hooks."""

    def __init__(self) -> None:
        self._startup: list[tuple[str, Hook]] = []
        self._shutdown: list[tuple[str, Hook]] = []

    def add_startup(self, name: str, hook: Hook) -> None:
        self._startup.append((name, hook))

    def add_shutdown(self, name: str, hook: Hook) -> None:
        self._shutdown.append((name, hook))

    async def run_startup(self) -> None:
        """Fire every startup hook in registration order.

        Raises:
            ConfigError: If a hook raises — startup runs before the server
                accepts traffic, so a failed hook stops the process.
        """
        for name, hook in self._startup:
            try:
                await _call(hook)
            except Exception as exc:
                raise ConfigError(f"plugin startup hook {name!r} failed: {exc}") from exc

    async def run_shutdown(self) -> None:
        """Fire every shutdown hook in reverse order, best-effort.

        Errors are logged and swallowed so one plugin's cleanup failure cannot
        prevent the others from running.
        """
        for name, hook in reversed(self._shutdown):
            try:
                await _call(hook)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("plugin shutdown hook %r failed: %s", name, exc)


async def _call(hook: Hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result
