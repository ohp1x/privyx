"""Events: what happens in Privyx, and the listeners that hear it.

An :class:`Interceptable` happens inside a flow, and a listener that fails
stops the flow.  A :class:`Notification` is a fact after it happened, without
payload content; a listener that fails is logged, and the others still run.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from privyx.core._errors import PrivyxError

logger = logging.getLogger("privyx.events")

#: A listener: called with the event, sync or async.
Listener = Callable[[Any], Awaitable[None] | None]


class Event:
    """Something that happens; listeners subscribe to its class or a base class."""

    #: The event's name, such as ``app.starting``.
    name: ClassVar[str]


class Interceptable(Event):
    """An event inside a flow: a listener that fails stops the flow."""


class Notification(Event):
    """A fact after it happened, without payload content: a listener that fails is logged."""


class ListenerError(PrivyxError):
    """A listener of an :class:`Interceptable` failed, so the flow stops."""

    def __init__(self, listener: str, event: Event, error: Exception) -> None:
        super().__init__(f"{listener} failed on {event.name}: {error}")
        self.listener = listener


class EventBus:
    """An app's listeners, by event class: the highest priority runs first.

    Listeners of equal priority run in the order they were added.  A listener
    of a base class, such as :class:`Notification`, hears every subclass.
    """

    def __init__(self) -> None:
        self._added: list[tuple[type[Event], int, Listener]] = []
        self._ordered: dict[type[Event], tuple[Listener, ...]] = {}

    def add(self, event: type[Event], listener: Listener, *, priority: int = 0) -> None:
        """Call ``listener`` on every ``event`` from the next one emitted."""
        self._added.append((event, priority, listener))
        # A new dict: an emit under way keeps the listeners it started with.
        self._ordered = {}

    def wants(self, event: type[Event]) -> bool:
        """Whether a listener hears ``event``: an event is built only when one does."""
        return bool(self._listeners(event))

    async def emit(self, event: Event) -> None:
        """Call each listener of ``event`` in turn, awaiting an async one in place.

        Raises:
            ListenerError: If a listener of an :class:`Interceptable` fails.
        """
        for listener in self._listeners(type(event)):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                if isinstance(event, Interceptable):
                    raise ListenerError(_name(listener), event, exc) from exc
                logger.warning("%s failed on %s: %s", _name(listener), event.name, exc)

    def _listeners(self, event: type[Event]) -> tuple[Listener, ...]:
        """The listeners of ``event`` in calling order, computed once per change."""
        found = self._ordered.get(event)
        if found is None:
            matching = [
                (-priority, order, listener)
                for order, (kind, priority, listener) in enumerate(self._added)
                if issubclass(event, kind)
            ]
            matching.sort(key=lambda entry: entry[:2])
            found = self._ordered[event] = tuple(listener for *_, listener in matching)
        return found


def _name(listener: Listener) -> str:
    return str(getattr(listener, "__qualname__", repr(listener)))
