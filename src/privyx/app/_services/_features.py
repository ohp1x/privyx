"""``app.features``: which features are on, switching them, and the listeners they add."""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from privyx.core import listened

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.config.schema import Settings
    from privyx.core import Services
    from privyx.features import Feature
    from privyx.observability.audit import AuditLogger


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

    def configured(self, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
        """The features ``settings`` switch on, by name, with their options.

        A feature on by default is on, and the settings of the built-ins
        switch theirs: ``audit.enabled`` and ``audit.path``, ``tracing.enabled``,
        ``proxy.directive``, ``proxy.cache_control``.  ``budget`` is a
        ``privyx run`` profile's.
        """
        from privyx.features import Feature

        settings = settings if settings is not None else self._app.settings
        registry = self._app.registry(Feature)
        on: dict[str, dict[str, Any]] = {
            name: {} for name in registry.names() if registry.get(name).enabled_by_default
        }
        on.pop("audit", None)
        if settings.audit.enabled:
            on["audit"] = {"path": settings.audit.path}
        for name, switched in (
            ("tracing", settings.tracing.enabled),
            ("directive", settings.proxy.directive),
            ("cache_control", settings.proxy.cache_control),
        ):
            if switched:
                on[name] = {}
        return on

    def list(self, settings: Settings | None = None) -> list[FeatureInfo]:
        """Every registered feature type, sorted, whether the settings switch it on."""
        from privyx.features import Feature

        registry = self._app.registry(Feature)
        on = self.configured(settings)
        found = []
        for name in registry.names():
            listens = tuple(
                (event.name, priority) for event, priority, _ in listened(registry.get(name))
            )
            found.append(FeatureInfo(name, name in on, listens, registry.extends(name)))
        return found

    def get(self, name: str) -> Feature | None:
        """The running feature ``name``, if it is on."""
        running = self._on.get(name)
        return running[0] if running is not None else None

    async def enable(self, name: str, **options: Any) -> Feature:
        """Build, open, and switch on feature ``name``, from the next exchange.

        Only in this process, and not saved: the settings decide at the next start.

        Raises:
            ConfigError: If it is unknown, an option invalid, or it cannot open.
        """
        from privyx.features import Feature

        await self.disable(name)
        feature = self._app.registry(Feature).build(name, options, setting=f"features.{name}")
        await feature.open(self._services or self._shared(self._app.settings))
        added = []
        for event, priority, listener in listened(feature):
            self._app._events.add(event, listener, priority=priority)
            added.append(listener)
        self._on[name] = (feature, added)
        return feature

    async def disable(self, name: str) -> None:
        """Switch off feature ``name`` and close it, if it is on."""
        running = self._on.pop(name, None)
        if running is not None:
            feature, added = running
            for listener in added:
                self._app._events.remove(listener)
            await feature.close()

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
        them to those names.  They are switched off in reverse order.

        Raises:
            ConfigError: If one cannot be built or opened.
        """
        from privyx.observability.audit import AuditLogger

        wanted = {**self.configured(settings), **(extra or {})}
        started: list[str] = []
        self._services = self._shared(settings)
        try:
            for name, options in wanted.items():
                if only is None or name in only:
                    await self.enable(name, **options)
                    started.append(name)
            yield AuditLogger(events=self._app._events)
        finally:
            for name in reversed(started):
                await self.disable(name)
            self._services = None

    def _shared(self, settings: Settings) -> Services:
        """What the app shares with a feature it opens under ``settings``."""
        from privyx.app._build import _services, build_codec

        return _services(settings, self._app, build_codec(settings, self._app), None)
