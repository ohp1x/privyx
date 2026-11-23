"""``app.features``: which features are on, switching them, and the listeners they add."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from privyx.core import ConfigError, listened

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.config.schema import Settings
    from privyx.core import Services
    from privyx.features import Feature
    from privyx.observability.audit import AuditLogger

logger = logging.getLogger("privyx.features")

#: Seconds a stopping server waits for the calls that hold a feature being closed.
_CLOSE_WAIT = 5.0


@dataclass(frozen=True)
class FeatureInfo:
    """A feature type: whether the settings switch it on, and what it listens to."""

    name: str
    enabled: bool
    #: Each event it listens to, with the listener's priority.
    listens: tuple[tuple[str, int], ...]
    #: The registered type it extends, if any.
    extends: str | None = None


class Features:
    """The features of one app: which are on, and those running."""

    def __init__(self, app: Privyx) -> None:
        self._app = app
        #: The features running, by name, with the listeners they added.
        self._on: dict[str, tuple[Feature, list[Any]]] = {}
        #: What the features opened while the app serves get.
        self._services: Services | None = None
        #: Features switched off, closing once the calls that still hold them end.
        self._closing: set[asyncio.Task[None]] = set()
        self._waiting: set[asyncio.Task[None]] = set()
        #: Enables and disables are numbered; an enable still opening gives way
        #: to a later call that took effect for its name, or to the app stopping.
        self._turn = 0
        self._settled: dict[str, int] = {}
        self._stopped_at = 0
        self._stopping = False

    def configured(self, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
        """The features ``settings`` switch on, by name, with their options.

        An entry of ``features`` is on unless it says ``enabled: false``; a
        feature the settings do not name is on when its type is on by default.
        ``budget`` is a ``privyx run`` profile's.
        """
        from privyx.features import Feature

        settings = settings if settings is not None else self._app.settings
        registry = self._app.registry(Feature)
        on: dict[str, dict[str, Any]] = {
            name: {}
            for name in registry.names()
            if name not in settings.features and registry.get(name).enabled_by_default
        }
        for name, entry in settings.features.items():
            if entry.enabled:
                kind = {"type": entry.type} if entry.type != name else {}
                on[name] = {**kind, **(entry.model_extra or {})}
        return on

    def options(self, name: str, settings: Settings | None = None) -> Any:
        """The options feature ``name`` has in ``settings``, checked: its type's ``Options``.

        Raises:
            ConfigError: If its type is unknown, or an option invalid.
        """
        from privyx.features import Feature

        settings = settings if settings is not None else self._app.settings
        entry = settings.features.get(name)
        kind = entry.type if entry is not None else name
        options = (entry.model_extra or {}) if entry is not None else {}
        return self._app.registry(Feature).validate(kind, options, setting=f"features.{name}")

    def list(self, settings: Settings | None = None) -> list[FeatureInfo]:
        """Every feature type and entry of ``features``, sorted, whether the settings switch it on.

        Raises:
            ConfigError: If an entry names a type no plugin added.
        """
        from privyx.features import Feature

        settings = settings if settings is not None else self._app.settings
        registry = self._app.registry(Feature)
        on = self.configured(settings)
        found = []
        for name in sorted({*registry.names(), *settings.features}):
            entry = settings.features.get(name)
            kind = entry.type if entry is not None else name
            listens = tuple(
                (event.name, priority) for event, priority, _ in listened(registry.get(kind))
            )
            extends = kind if kind != name else registry.extends(name)
            found.append(FeatureInfo(name, name in on, listens, extends))
        return found

    def get(self, name: str) -> Feature | None:
        """The running feature ``name``, if it is on."""
        running = self._on.get(name)
        return running[0] if running is not None else None

    async def enable(self, name: str, **options: Any) -> Feature:
        """Build, open, and switch on feature ``name``, from the next exchange.

        Only in this process, and not saved: the settings decide at the next start.
        What runs now keeps running if this fails.  Of the calls for a name, a
        later one that took effect wins: an earlier one still opening then
        closes what it opened.

        Raises:
            ConfigError: If it is unknown, an option invalid, it cannot open,
                or a later call or the app stopping overtook it.
        """
        from privyx.features import Feature

        if self._stopping:
            raise ConfigError(f"features.{name}: the app is stopping")
        options = dict(options)
        kind = options.pop("type", name)
        feature = self._app.registry(Feature).build(kind, options, setting=f"features.{name}")
        turn = self._next_turn()
        await feature.open(self._services or self._shared(self._app.settings))
        if turn < max(self._settled.get(name, 0), self._stopped_at):
            await self._close(feature)
            raise ConfigError(
                f"features.{name}: a later change, or the app stopping, took over while it opened"
            )
        self._settled[name] = turn
        # Nothing is awaited from here until the old one is off, so every event
        # finds exactly one of them.
        added = [
            self._app._events.add(event, listener, priority=priority)
            for event, priority, listener in listened(feature)
        ]
        replaced = self._on.get(name)
        self._on[name] = (feature, added)
        if replaced is not None:
            await self._stop(*replaced)
        return feature

    async def disable(self, name: str) -> None:
        """Switch off feature ``name`` and close it, if it is on."""
        self._settled[name] = self._next_turn()
        running = self._on.pop(name, None)
        if running is not None:
            await self._stop(*running)

    def _next_turn(self) -> int:
        """The number of a new enable or disable, in the order they were called."""
        self._turn += 1
        return self._turn

    async def _stop(self, feature: Feature, added: Collection[Any]) -> None:
        """Remove the listeners of ``feature``, and close it.

        A call under way that holds one of them keeps it: the feature then
        closes once that call ends, without holding up the caller.  Otherwise
        it is closed on return.  A cancelled caller does not interrupt either.
        """
        events = self._app._events
        calls = events.under_way(added)
        for entry in added:
            events.discard(entry)
        waiting = None
        if calls:
            waiting = asyncio.create_task(events.finished(calls))
            self._waiting.add(waiting)
            waiting.add_done_callback(self._waiting.discard)
        closing = asyncio.create_task(self._close_after(feature, waiting))
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)
        if waiting is None:
            await asyncio.shield(closing)

    async def _close_after(self, feature: Feature, waiting: asyncio.Task[None] | None) -> None:
        """Close ``feature`` once ``waiting`` ends, or a stopping app gives up on it."""
        if waiting is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await waiting
        await self._close(feature)

    @staticmethod
    async def _close(feature: Feature) -> None:
        """Close ``feature``, already off: a failure is logged, and stops nothing else."""
        try:
            await feature.close()
        except Exception as exc:
            logger.warning("feature %s failed to close: %s", feature.name, exc)

    async def _closed(self) -> None:
        """Wait for the features still closing; only the wait for their calls is bounded."""
        if self._waiting:
            _, late = await asyncio.wait(self._waiting, timeout=_CLOSE_WAIT)
            for task in late:
                task.cancel()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    @asynccontextmanager
    async def running(
        self,
        settings: Settings,
        *,
        only: Collection[str] | None = None,
        extra: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> AsyncIterator[AuditLogger]:
        """Switch on the features ``settings`` configure, and ``extra`` ones, until exit.

        Yields the audit logger whose notifications they hear.  ``only`` limits
        them to those names.  They are switched off in reverse order, with
        any feature enabled meanwhile.

        Raises:
            ConfigError: If one cannot be built or opened.
        """
        from privyx.observability.audit import AuditLogger

        wanted = {**self.configured(settings), **(extra or {})}
        started: list[str] = []
        before = set(self._on)
        self._services = self._shared(settings)
        try:
            for name, options in wanted.items():
                if only is None or name in only:
                    await self.enable(name, **options)
                    started.append(name)
            yield AuditLogger(events=self._app._events)
        finally:
            self._stopping = True
            self._stopped_at = self._next_turn()  # an enable still opening gives way
            try:
                for name in reversed([*self._on]):
                    if name in started or name not in before:
                        await self.disable(name)
                await self._closed()
            finally:
                self._services = None
                self._stopping = False

    def _shared(self, settings: Settings) -> Services:
        """What the app shares with a feature it opens under ``settings``."""
        from privyx.app._build import _services, build_codec

        return _services(self._app, build_codec(settings, self._app), None)
