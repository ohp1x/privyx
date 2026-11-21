"""``EventBus``: listeners by priority, and what a failing listener does to each kind of event."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

import pytest

from privyx.core import (
    Event,
    EventBus,
    Interceptable,
    ListenerError,
    Notification,
    Rejected,
    Responded,
    listen,
    listened,
)


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


async def test_reject_and_respond_end_the_flow_and_skip_the_listeners_after() -> None:
    bus = EventBus()
    heard: list[str] = []
    bus.add(Started, lambda event: event.reject(400, "too_big", "no"), priority=10)
    bus.add(Started, lambda event: heard.append("after"))

    with pytest.raises(Rejected) as rejected:
        await bus.emit(Started())
    assert (rejected.value.status, rejected.value.code, rejected.value.message) == (
        400,
        "too_big",
        "no",
    )
    assert heard == []

    answer = EventBus()
    answer.add(Started, lambda event: event.respond(200, {"ok": True}, {"x-a": "1"}))
    with pytest.raises(Responded) as responded:
        await answer.emit(Started())
    assert (responded.value.body, responded.value.headers) == ({"ok": True}, {"x-a": "1"})


async def test_a_failing_listener_is_named_by_its_component() -> None:
    class Tagger:
        name = "acme-tagger"

        def tag(self, event: Started) -> None:
            raise ValueError("boom")

    bus = EventBus()
    bus.add(Started, Tagger().tag)

    with pytest.raises(ListenerError) as failed:
        await bus.emit(Started())
    assert failed.value.listener == "acme-tagger"


async def test_a_removed_listener_hears_nothing_more() -> None:
    bus = EventBus()
    heard: list[str] = []

    def hear(event: Noted) -> None:
        heard.append(event.name)

    bus.add(Noted, hear)
    await bus.emit(Noted())
    bus.remove(hear)
    await bus.emit(Noted())

    assert heard == ["test.noted"]
    assert not bus.wants(Noted)


async def test_notify_calls_sync_listeners_and_runs_an_async_one_as_a_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    heard: list[str] = []

    async def later(event: Noted) -> None:
        heard.append("async")

    def broken(event: Noted) -> None:
        raise RuntimeError("boom")

    bus.add(Noted, later)
    bus.add(Noted, broken)
    bus.add(Noted, lambda event: heard.append("sync"))

    with caplog.at_level(logging.WARNING, logger="privyx.events"):
        bus.notify(Noted())
    assert heard == ["sync"]
    await asyncio.sleep(0)
    assert heard == ["sync", "async"]
    assert "broken failed on test.noted" in caplog.text


def test_listen_marks_methods_and_listened_finds_them_bound() -> None:
    class Feature:
        @listen(Started, priority=100)
        def check(self, event: Started) -> None: ...

        @listen(Noted)
        def count(self, event: Noted) -> None: ...

    owner = Feature()
    found = {(event, priority): listener for event, priority, listener in listened(owner)}

    assert set(found) == {(Started, 100), (Noted, 0)}
    assert found[(Started, 100)] == owner.check
    assert [(e, p) for e, p, _ in listened(Feature)] == [(Started, 100), (Noted, 0)]
