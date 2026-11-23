"""``app.features.enable()`` over a feature already running."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from privyx.app import Privyx
from privyx.app._services import _features as features_service
from privyx.config.schema import Settings
from privyx.core import ConfigError, ListenerError, listen
from privyx.events import AppStarting
from privyx.features import Feature
from privyx.features._tracing import Tracing


def _owners(app: Privyx) -> list[object]:
    """The object behind each listener the app has."""
    return [getattr(listener, "__self__", None) for _, _, listener in app._events._added]


async def test_a_refused_option_leaves_the_running_feature_on() -> None:
    app = Privyx()
    budget = await app.features.enable("budget", max_turns=1)

    with pytest.raises(ConfigError):
        await app.features.enable("budget", max_turns=0)

    assert app.features.get("budget") is budget
    assert budget in _owners(app)
    await app.features.disable("budget")


async def test_a_replacement_that_cannot_open_leaves_the_running_feature_on(
    tmp_path: Path,
) -> None:
    app = Privyx()
    audit = await app.features.enable("audit", path=str(tmp_path / "a.log"))
    (tmp_path / "file").write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="cannot write"):
        await app.features.enable("audit", path=str(tmp_path / "file" / "a.log"))

    assert app.features.get("audit") is audit
    assert audit in _owners(app)
    await app.features.disable("audit")


async def test_a_replacement_takes_over_every_listener() -> None:
    app = Privyx()
    old = await app.features.enable("budget", max_turns=1)
    new = await app.features.enable("budget", max_turns=5)

    assert app.features.get("budget") is new
    assert old not in _owners(app)
    assert new in _owners(app)
    await app.features.disable("budget")
    assert new not in _owners(app)


async def test_the_replacement_listens_before_the_old_one_closes() -> None:
    app = Privyx()
    old = await app.features.enable("budget", max_turns=1)
    original = old.close
    at_close: list[tuple[bool, bool]] = []

    async def close() -> None:
        running = app.features.get("budget")
        at_close.append((running is not old, running in _owners(app)))
        await original()

    old.close = close  # type: ignore[method-assign]
    await app.features.enable("budget", max_turns=2)

    assert at_close == [(True, True)]
    await app.features.disable("budget")


class _Shared(Feature):
    """Its listener is one function, whichever instance adds it."""

    name = "shared"
    heard: ClassVar[list[str]] = []

    @staticmethod
    @listen(AppStarting)
    def on_start(event: AppStarting) -> None:
        _Shared.heard.append(event.name)


async def test_a_replacement_keeps_a_listener_both_instances_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_Shared, "heard", [])
    app = Privyx()
    app.add(_Shared)
    await app.features.enable("shared")
    await app._events.emit(AppStarting())

    await app.features.enable("shared")
    await app._events.emit(AppStarting())

    assert len(_Shared.heard) == 2
    await app.features.disable("shared")
    await app._events.emit(AppStarting())
    assert len(_Shared.heard) == 2


async def test_disabling_one_entry_leaves_the_listener_of_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_Shared, "heard", [])
    app = Privyx()
    app.add(_Shared)
    await app.features.enable("one", type="shared")
    await app.features.enable("two", type="shared")

    await app.features.disable("one")
    await app._events.emit(AppStarting())

    assert len(_Shared.heard) == 1
    await app.features.disable("two")


async def test_disabling_one_entry_keeps_the_order_of_the_others() -> None:
    def middle(event: AppStarting) -> None:
        pass

    app = Privyx()
    app.add(_Shared)
    await app.features.enable("one", type="shared")
    app._events.add(AppStarting, middle)
    await app.features.enable("two", type="shared")

    await app.features.disable("two")

    assert app._events.listeners(AppStarting) == (_Shared.on_start, middle)
    await app.features.disable("one")


class _Guarded(Feature):
    """Fails if it hears an event once closed."""

    name = "guarded"
    closed = False
    calls = 0

    async def close(self) -> None:
        self.closed = True

    @listen(AppStarting)
    def on_start(self, event: AppStarting) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.calls += 1


async def test_a_call_under_way_finishes_before_the_replaced_feature_closes() -> None:
    app = Privyx()
    app.add(_Guarded)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event: AppStarting) -> None:
        entered.set()
        await release.wait()

    app._events.add(AppStarting, hold, priority=10)
    old = await app.features.enable("guarded")
    under_way = asyncio.create_task(app._events.emit(AppStarting()))
    await entered.wait()

    new = await app.features.enable("guarded")

    assert not old.closed
    release.set()
    await under_way  # no ListenerError from a closed feature
    await asyncio.gather(*list(app.features._closing))
    assert old.closed
    assert (old.calls, new.calls) == (1, 0)
    await app.features.disable("guarded")


async def test_events_after_the_replacement_do_not_delay_the_close() -> None:
    app = Privyx()
    app.add(_Guarded)
    gates = [asyncio.Event(), asyncio.Event()]
    next_gate = iter(gates)

    async def hold(event: AppStarting) -> None:
        await next(next_gate).wait()

    app._events.add(AppStarting, hold, priority=10)
    old = await app.features.enable("guarded")
    first = asyncio.create_task(app._events.emit(AppStarting()))
    await asyncio.sleep(0)
    await app.features.enable("guarded")
    second = asyncio.create_task(app._events.emit(AppStarting()))
    await asyncio.sleep(0)

    gates[0].set()
    await first
    await asyncio.wait_for(asyncio.gather(*list(app.features._closing)), 1)

    assert old.closed
    gates[1].set()
    await second
    await app.features.disable("guarded")


async def test_leaving_running_closes_a_feature_a_call_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(features_service, "_CLOSE_WAIT", 0.01)
    app = Privyx()
    app.add(_Guarded)
    release = asyncio.Event()

    async def hold(event: AppStarting) -> None:
        await release.wait()

    app._events.add(AppStarting, hold, priority=10)
    async with app.features.running(Settings(features={"audit": {"enabled": False}})):
        old = await app.features.enable("guarded")
        under_way = asyncio.create_task(app._events.emit(AppStarting()))
        await asyncio.sleep(0)
        await app.features.disable("guarded")
        assert not old.closed

    assert old.closed
    release.set()
    with pytest.raises(ListenerError):
        await under_way


async def test_a_feature_enabled_before_the_server_outlives_it() -> None:
    app = Privyx()
    directive = await app.features.enable("directive")

    async with app.features.running(
        Settings(features={"audit": {"enabled": False}, "directive": {"enabled": False}})
    ):
        await app.features.enable("budget", max_turns=1)

    assert app.features.get("directive") is directive
    assert app.features.get("budget") is None
    await app.features.disable("directive")


async def test_an_event_right_after_the_replacement_does_not_delay_the_close() -> None:
    app = Privyx()
    app.add(_Guarded)
    gates = [asyncio.Event(), asyncio.Event()]
    next_gate = iter(gates)

    async def hold(event: AppStarting) -> None:
        await next(next_gate).wait()

    app._events.add(AppStarting, hold, priority=10)
    old = await app.features.enable("guarded")
    first = asyncio.create_task(app._events.emit(AppStarting()))
    await asyncio.sleep(0)
    await app.features.enable("guarded")
    # Started before the task that closes the old one has run at all.
    loop = asyncio.get_running_loop()
    second = asyncio.Task(app._events.emit(AppStarting()), loop=loop, eager_start=True)

    gates[0].set()
    await first
    await asyncio.wait_for(asyncio.gather(*list(app.features._closing)), 1)

    assert old.closed
    gates[1].set()
    await second
    await app.features.disable("guarded")


class _SlowClose(Feature):
    """Its close takes until ``release`` is set."""

    name = "slow_close"
    release: ClassVar[asyncio.Event]
    began: ClassVar[asyncio.Event]
    finished = False

    async def close(self) -> None:
        self.began.set()
        await self.release.wait()
        self.finished = True

    @listen(AppStarting)
    def on_start(self, event: AppStarting) -> None:
        pass


async def test_a_stopping_server_lets_a_close_under_way_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(features_service, "_CLOSE_WAIT", 0.01)
    monkeypatch.setattr(_SlowClose, "release", asyncio.Event(), raising=False)
    monkeypatch.setattr(_SlowClose, "began", asyncio.Event(), raising=False)
    app = Privyx()
    app.add(_SlowClose)
    entered, go = asyncio.Event(), asyncio.Event()

    async def hold(event: AppStarting) -> None:
        entered.set()
        await go.wait()

    app._events.add(AppStarting, hold, priority=10)
    async with app.features.running(Settings(features={"audit": {"enabled": False}})):
        old = await app.features.enable("slow_close")
        under_way = asyncio.create_task(app._events.emit(AppStarting()))
        await entered.wait()
        await app.features.disable("slow_close")
        go.set()
        await under_way
        await _SlowClose.began.wait()
        asyncio.get_running_loop().call_later(0.05, _SlowClose.release.set)

    assert old.finished


class _SlowOpen(Feature):
    """The first one made opens only once ``gate`` is set; ``refuse`` fails the next open."""

    name = "slow_open"
    gate: ClassVar[asyncio.Event]
    opening: ClassVar[asyncio.Event]
    made: ClassVar[list[_SlowOpen]]
    refuse: ClassVar[bool] = False
    closed = False

    async def open(self, services: object) -> None:
        if _SlowOpen.refuse:
            _SlowOpen.refuse = False
            raise ConfigError("cannot open")
        _SlowOpen.made.append(self)
        if len(_SlowOpen.made) == 1:
            _SlowOpen.opening.set()
            await _SlowOpen.gate.wait()

    async def close(self) -> None:
        self.closed = True

    @listen(AppStarting)
    def on_start(self, event: AppStarting) -> None:
        pass


@pytest.fixture
def slow_open(monkeypatch: pytest.MonkeyPatch) -> Privyx:
    monkeypatch.setattr(_SlowOpen, "gate", asyncio.Event(), raising=False)
    monkeypatch.setattr(_SlowOpen, "opening", asyncio.Event(), raising=False)
    monkeypatch.setattr(_SlowOpen, "made", [], raising=False)
    monkeypatch.setattr(_SlowOpen, "refuse", False)
    app = Privyx()
    app.add(_SlowOpen)
    return app


async def test_an_activation_still_opening_when_the_server_stops_is_closed(
    slow_open: Privyx,
) -> None:
    async with slow_open.features.running(Settings(features={"audit": {"enabled": False}})):
        task = asyncio.create_task(slow_open.features.enable("slow_open"))
        await _SlowOpen.opening.wait()

    _SlowOpen.gate.set()
    with pytest.raises(ConfigError, match="features.slow_open"):
        await task

    assert _SlowOpen.made[0].closed
    assert slow_open.features.get("slow_open") is None
    assert slow_open._events.listeners(AppStarting) == ()


async def test_no_feature_starts_while_the_server_stops(slow_open: Privyx) -> None:
    started: list[asyncio.Task[Feature]] = []

    async with slow_open.features.running(Settings(features={"audit": {"enabled": False}})):
        budget = await slow_open.features.enable("budget", max_turns=1)
        original = budget.close

        async def close() -> None:
            started.append(asyncio.create_task(slow_open.features.enable("directive")))
            await asyncio.sleep(0)
            await original()

        budget.close = close  # type: ignore[method-assign]

    with pytest.raises(ConfigError, match=r"features\.directive"):
        await started[0]
    assert slow_open.features.get("directive") is None


async def test_a_later_enable_wins_over_one_still_opening(slow_open: Privyx) -> None:
    first = asyncio.create_task(slow_open.features.enable("slow_open"))
    await _SlowOpen.opening.wait()
    second = await slow_open.features.enable("slow_open")

    _SlowOpen.gate.set()
    with pytest.raises(ConfigError, match="features.slow_open"):
        await first

    assert _SlowOpen.made[0].closed
    assert slow_open.features.get("slow_open") is second
    assert slow_open._events.listeners(AppStarting) == (second.on_start,)
    await slow_open.features.disable("slow_open")


async def test_a_disable_wins_over_an_enable_still_opening(slow_open: Privyx) -> None:
    first = asyncio.create_task(slow_open.features.enable("slow_open"))
    await _SlowOpen.opening.wait()
    await slow_open.features.disable("slow_open")

    _SlowOpen.gate.set()
    with pytest.raises(ConfigError, match="features.slow_open"):
        await first

    assert _SlowOpen.made[0].closed
    assert slow_open.features.get("slow_open") is None


class _Staged(Feature):
    """Each one opens when its future in ``steps`` is done, or fails with it."""

    name = "staged"
    steps: ClassVar[list[asyncio.Future[None]]]
    made: ClassVar[list[_Staged]]
    closed = False

    async def open(self, services: object) -> None:
        step = _Staged.steps[len(_Staged.made)]
        _Staged.made.append(self)
        await step

    async def close(self) -> None:
        self.closed = True


async def test_two_refused_enables_do_not_stop_an_earlier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    first_step, second_step, third_step = (loop.create_future() for _ in range(3))
    monkeypatch.setattr(_Staged, "steps", [first_step, second_step, third_step], raising=False)
    monkeypatch.setattr(_Staged, "made", [], raising=False)
    app = Privyx()
    app.add(_Staged)
    first, second, third = (asyncio.create_task(app.features.enable("staged")) for _ in range(3))
    await asyncio.sleep(0)

    second_step.set_exception(ConfigError("second"))
    with pytest.raises(ConfigError, match="second"):
        await second
    third_step.set_exception(ConfigError("third"))
    with pytest.raises(ConfigError, match="third"):
        await third
    first_step.set_result(None)
    running = await first

    assert app.features.get("staged") is running
    assert not running.closed
    await app.features.disable("staged")


async def test_an_unrelated_event_does_not_delay_a_feature_without_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("privyx.observability.tracing.configure", calls.append)
    monkeypatch.setattr(Tracing, "_open", 0)
    app = Privyx()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event: AppStarting) -> None:
        entered.set()
        await release.wait()

    app._events.add(AppStarting, hold)
    await app.features.enable("tracing")
    under_way = asyncio.create_task(app._events.emit(AppStarting()))
    await entered.wait()

    await app.features.disable("tracing")

    assert calls[-1] is False
    release.set()
    await under_way


async def test_a_call_that_cannot_reach_the_feature_does_not_delay_its_close() -> None:
    app = Privyx()
    app.add(_Guarded)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event: AppStarting) -> None:
        entered.set()
        await release.wait()

    app._events.add(AppStarting, hold)
    under_way = asyncio.create_task(app._events.emit(AppStarting()))
    await entered.wait()  # it started before the feature was on
    feature = await app.features.enable("guarded")

    await app.features.disable("guarded")

    assert feature.closed
    release.set()
    await under_way
    assert feature.calls == 0


async def test_a_cancelled_caller_does_not_interrupt_a_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_SlowClose, "release", asyncio.Event(), raising=False)
    monkeypatch.setattr(_SlowClose, "began", asyncio.Event(), raising=False)
    app = Privyx()
    app.add(_SlowClose)
    feature = await app.features.enable("slow_close")

    disabling = asyncio.create_task(app.features.disable("slow_close"))
    await _SlowClose.began.wait()
    disabling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disabling

    _SlowClose.release.set()
    await asyncio.gather(*list(app.features._closing))
    assert feature.finished


async def test_a_replaced_feature_that_fails_to_close_leaves_the_new_one_on() -> None:
    app = Privyx()
    old = await app.features.enable("budget", max_turns=1)

    async def fails() -> None:
        raise RuntimeError("cannot close")

    old.close = fails  # type: ignore[method-assign]
    new = await app.features.enable("budget", max_turns=2)

    assert app.features.get("budget") is new
    await app.features.disable("budget")


async def test_a_feature_that_fails_to_close_does_not_keep_the_others_open() -> None:
    app = Privyx()
    closed: list[str] = []
    async with app.features.running(Settings(features={"audit": {"enabled": False}})):
        first = await app.features.enable("budget", max_turns=1)
        second = await app.features.enable("directive")
        original = first.close

        async def tracked() -> None:
            closed.append("budget")
            await original()

        async def fails() -> None:
            raise RuntimeError("cannot close")

        first.close = tracked  # type: ignore[method-assign]
        second.close = fails  # type: ignore[method-assign]

    assert closed == ["budget"]
    assert app.features.get("budget") is None
    assert app.features.get("directive") is None


async def test_a_refused_enable_does_not_stop_one_still_opening(slow_open: Privyx) -> None:
    first = asyncio.create_task(slow_open.features.enable("slow_open"))
    await _SlowOpen.opening.wait()
    _SlowOpen.refuse = True
    with pytest.raises(ConfigError, match="cannot open"):
        await slow_open.features.enable("slow_open")

    _SlowOpen.gate.set()
    running = await first

    assert slow_open.features.get("slow_open") is running
    assert not running.closed
    await slow_open.features.disable("slow_open")


async def test_tracing_stays_on_while_replaced_and_goes_off_with_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("privyx.observability.tracing.configure", calls.append)
    monkeypatch.setattr(Tracing, "_open", 0)
    app = Privyx()

    await app.features.enable("tracing")
    await app.features.enable("tracing")
    assert False not in calls

    await app.features.disable("tracing")
    assert calls[-1] is False
