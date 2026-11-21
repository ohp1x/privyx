"""Events: what happens in Privyx, and the listeners that hear it.

An :class:`Interceptable` happens inside a flow, and a listener that fails
stops the flow.  A :class:`Notification` is a fact after it happened, without
payload content; a listener that fails is logged, and the others still run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, NoReturn

from privyx.core._errors import PrivyxError

logger = logging.getLogger("privyx.events")

#: A listener: called with the event, sync or async.
Listener = Callable[[Any], Awaitable[None] | None]


class Event:
    """Something that happens; listeners subscribe to its class or a base class."""

    #: The event's name, such as ``app.starting``.
    name: ClassVar[str]


class Stop(Exception):  # noqa: N818 - control flow, not a failure
    """A listener ended a flow on purpose: :meth:`Interceptable.reject` or ``respond``."""


class Rejected(Stop):
    """A listener refused the exchange: answered with an error in the client's format."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


class Responded(Stop):
    """A listener answered the exchange itself."""

    def __init__(self, status: int, body: Any, headers: Mapping[str, str] | None) -> None:
        super().__init__(f"answered with {status}")
        self.status, self.body, self.headers = status, body, dict(headers or {})


class Interceptable(Event):
    """An event inside a flow: a listener that fails stops the flow.

    A listener may also end the flow on purpose, with :meth:`reject` or
    :meth:`respond`; the listeners after it do not run.
    """

    def reject(self, status: int, code: str, message: str) -> NoReturn:
        """Refuse the exchange with ``status``: an error with ``code`` and ``message``."""
        raise Rejected(status, code, message)

    def respond(self, status: int, body: Any, headers: Mapping[str, str] | None = None) -> NoReturn:
        """Answer the exchange with ``body`` (JSON) instead of sending it upstream."""
        raise Responded(status, body, headers)


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

    def remove(self, listener: Listener) -> None:
        """Stop calling ``listener``, from the next event emitted."""
        self._added = [entry for entry in self._added if entry[2] != listener]
        self._ordered = {}

    def listeners(self, event: type[Event]) -> tuple[Listener, ...]:
        """The listeners of ``event``, in the order they are called."""
        return self._listeners(event)

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
            except Stop:
                raise
            except Exception as exc:
                if isinstance(event, Interceptable):
                    raise ListenerError(_name(listener), event, exc) from exc
                logger.warning("%s failed on %s: %s", _name(listener), event.name, exc)

    def notify(self, event: Notification) -> None:
        """Emit a notification from code that does not await: each listener in turn.

        An async listener runs as a task of its own when a loop is running;
        a listener that fails is logged, and the others still run.
        """
        for listener in self._listeners(type(event)):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    _detach(result, listener, event)
            except Exception as exc:
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


def listen[F: Callable[..., Any]](event: type[Event], *, priority: int = 0) -> Callable[[F], F]:
    """Mark a feature's method as a listener of ``event`` and its subclasses.

    The app adds it when the feature is switched on, and removes it when off.
    The highest ``priority`` runs first.
    """

    def mark(method: F) -> F:
        method.__privyx_listen__ = (event, priority)  # type: ignore[attr-defined]
        return method

    return mark


def listened(owner: object) -> list[tuple[type[Event], int, Listener]]:
    """The listeners :func:`listen` marked on the methods of ``owner``, an object or a class.

    Bound to ``owner`` when it is an object.
    """
    cls = owner if isinstance(owner, type) else type(owner)
    found = []
    for name in dir(cls):
        marked = getattr(getattr(cls, name, None), "__privyx_listen__", None)
        if marked is not None:
            event, priority = marked
            found.append((event, priority, getattr(owner, name)))
    return found


def _detach(result: Awaitable[Any], listener: Listener, event: Event) -> None:
    """Run ``result`` as a task of its own, its failure logged; closed without a loop."""

    async def run() -> None:
        try:
            await result
        except Exception as exc:
            logger.warning("%s failed on %s: %s", _name(listener), event.name, exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        if inspect.iscoroutine(result):
            result.close()


def _name(listener: Listener) -> str:
    """What a failure names: the component a listener is a method of, or the function."""
    owner = getattr(getattr(listener, "__self__", None), "name", None)
    if isinstance(owner, str):
        return owner
    return str(getattr(listener, "__qualname__", repr(listener)))
