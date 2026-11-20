"""``EventBus``: listeners by priority, and what a failing listener does to each kind of event."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from privyx.core import Event, EventBus, Interceptable, ListenerError, Notification


class Started(Interceptable):
    name: ClassVar[str] = "test.started"


class Noted(Notification):
    name: ClassVar[str] = "test.noted"


async def test_listeners_run_by_priority_then_in_the_order_added() -> None:
    bus = EventBus()
    heard: list[str] = []
    bus.add(Noted, lambda event: heard.append("first"))
    bus.add(Noted, lambda event: heard.append("high"), priority=100)
    bus.add(Noted, lambda event: heard.append("second"))
    bus.add(Noted, lambda event: heard.append("low"), priority=-10)

    await bus.emit(Noted())
    assert heard == ["high", "first", "second", "low"]


async def test_a_listener_of_a_base_class_hears_every_subclass() -> None:
    bus = EventBus()
    heard: list[str] = []
    bus.add(Notification, lambda event: heard.append(event.name))
    bus.add(Event, lambda event: heard.append("any " + event.name))

    await bus.emit(Noted())
    await bus.emit(Started())
    assert heard == ["test.noted", "any test.noted", "any test.started"]


async def test_an_async_listener_is_awaited_in_place() -> None:
    bus = EventBus()
    heard: list[str] = []

    async def later(event: Noted) -> None:
        heard.append("async")

    bus.add(Noted, later)
    bus.add(Noted, lambda event: heard.append("sync"))

    await bus.emit(Noted())
    assert heard == ["async", "sync"]


async def test_a_failing_listener_of_an_interceptable_stops_the_flow() -> None:
    bus = EventBus()
    heard: list[str] = []

    def refuse(event: Started) -> None:
        raise RuntimeError("no license")

    bus.add(Started, refuse)
    bus.add(Started, lambda event: heard.append("after"))

    with pytest.raises(ListenerError, match="refuse failed on test.started: no license") as error:
        await bus.emit(Started())
    assert error.value.listener.endswith("refuse")
    assert isinstance(error.value.__cause__, RuntimeError)
    assert heard == []


async def test_a_failing_listener_of_a_notification_is_logged_and_the_rest_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    heard: list[str] = []

    def broken(event: Noted) -> None:
        raise RuntimeError("disk full")

    bus.add(Noted, broken)
    bus.add(Noted, lambda event: heard.append("after"))

    with caplog.at_level(logging.WARNING, logger="privyx.events"):
        await bus.emit(Noted())
    assert heard == ["after"]
    assert "broken failed on test.noted: disk full" in caplog.text


def test_wants_says_whether_anyone_listens() -> None:
    bus = EventBus()
    assert not bus.wants(Noted)
    bus.add(Notification, lambda event: None)
    assert bus.wants(Noted)
    assert not bus.wants(Started)


async def test_a_listener_added_while_emitting_hears_the_next_event() -> None:
    bus = EventBus()
    heard: list[str] = []

    def add_another(event: Noted) -> None:
        heard.append("first")
        if len(heard) == 1:
            bus.add(Noted, lambda event: heard.append("added"))

    bus.add(Noted, add_another)
    await bus.emit(Noted())
    await bus.emit(Noted())
    assert heard == ["first", "first", "added"]
