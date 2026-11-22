"""The conformance suite of each kind, for pytest.

A plugin's test module subclasses the suite of its component's kind, names
the component and its options, and pytest runs every check::

    class TestAcmeIdDetector(DetectorSuite):
        component = AcmeIdDetector
        options = {"region": "EU"}
        samples = {"order ACME-EU-123456 shipped": ["ACME-EU-123456"]}

The checks are plain functions that run their coroutines themselves, so no
async plugin for pytest is needed.  A suite builds its component with
:meth:`~ComponentSuite.create` and opens it with :func:`services`; a subclass
overrides either for a component that needs more.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar, TextIO
from urllib.parse import urlsplit

from pydantic import ValidationError

from privyx.core import (
    Component,
    Context,
    Detection,
    Event,
    EventBus,
    Services,
    Session,
    VaultError,
    listened,
)
from privyx.events import AuditEvent
from privyx.features import Feature
from privyx.observability.audit import AuditLogger
from privyx.token import FormatCodec


def services(**changes: Any) -> Services:
    """What an app gives a component in ``open()``: the default codec, no anchor."""
    values: dict[str, Any] = {"codec": FormatCodec.default(), "anchor": None, "events": EventBus()}
    return Services(**{**values, **changes})


async def opened[C: Component](component: C, **changes: Any) -> C:
    """``component``, opened with :func:`services` and ``changes`` to them."""
    await component.open(services(**changes))
    return component


async def listening(*features: Component, **changes: Any) -> EventBus:
    """A bus the ``features`` listen on, each opened with :func:`services` and ``changes``.

    Hand it to a :class:`~privyx.proxy.Pipeline` as ``events``, with an
    :class:`~privyx.observability.audit.AuditLogger` on it, to run exchanges
    with the features on.
    """
    bus = changes.pop("events", None) or EventBus()
    for feature in features:
        await feature.open(services(events=bus, **changes))
        for event, priority, listener in listened(feature):
            bus.add(event, listener, priority=priority)
    return bus


def audit_to(stream: TextIO | None, *, events: EventBus | None = None) -> AuditLogger:
    """An audit logger on ``events`` that also writes each record to ``stream``.

    One JSON line per record, as the ``audit`` feature writes them.
    """
    bus = events or EventBus()
    if stream is not None:
        bus.add(AuditEvent, lambda event: print(event.to_json(), file=stream, flush=True))
    return AuditLogger(events=bus)


@contextmanager
def capture_events(app: Any, event: type[Event] = Event) -> Iterator[list[Event]]:
    """The events of class ``event`` that ``app`` emits while the block runs, in order.

    Heard after every other listener, so an interceptable event shows what
    they left; it is the event object itself, which a later step may change.
    """
    seen: list[Event] = []
    app._events.add(event, seen.append, priority=-(2**31))
    try:
        yield seen
    finally:
        app._events.remove(seen.append)


def run[T](make: Callable[[], Awaitable[T]]) -> T:
    """Run the coroutine ``make()`` returns, to its end."""

    async def main() -> T:
        return await make()

    return asyncio.run(main())


class ComponentSuite:
    """Builds the component under test: :attr:`component` with :attr:`options`."""

    component: ClassVar[type[Component]]
    #: The options the component is built with.
    options: ClassVar[dict[str, Any]] = {}

    def create(self, **options: Any) -> Any:
        """A new component, with :attr:`options` and ``options`` over them."""
        return self.component(**{**self.options, **options})

    async def opened(self, **options: Any) -> Any:
        """:meth:`create`, opened with :func:`services`."""
        component = self.create(**options)
        await component.open(services())
        return component

    def test_an_unknown_option_is_refused(self) -> None:
        try:
            self.create(privyx_suite_unknown_option=1)
        except ValidationError:
            return
        raise AssertionError("an unknown option was taken")


class DetectorSuite(ComponentSuite):
    """A detector: spans within the text and matching it, the same each time, its samples."""

    #: Texts, each with the values the detector must find in it.
    samples: ClassVar[dict[str, list[str]]] = {}

    def _detect(self, text: str) -> Detection:
        async def detect() -> Detection:
            detector = await self.opened()
            try:
                result: Detection = await detector.detect(text, Context())
                return result
            finally:
                await detector.close()

        return run(detect)

    def test_spans_are_within_the_text_and_match_it(self) -> None:
        for text in self.samples:
            for span in self._detect(text).spans:
                assert 0 <= span.start < span.end <= len(text), span
                assert text[span.start : span.end] == span.text, span

    def test_detection_is_deterministic(self) -> None:
        for text in self.samples:
            assert self._detect(text).spans == self._detect(text).spans

    def test_sync_and_async_agree(self) -> None:
        from privyx.detectors import Detector

        detector = self.create()
        if type(detector).detect_sync is Detector.detect_sync:
            return  # async only
        for text in self.samples:
            assert detector.detect_sync(text, Context()).spans == self._detect(text).spans

    def test_samples_are_found(self) -> None:
        for text, values in self.samples.items():
            found = {span.text for span in self._detect(text).spans}
            assert set(values) <= found, (text, found)


class PolicySuite(ComponentSuite):
    """A policy: decides among the detected spans only, the same each time."""

    #: Spans to decide on, as ``(text, entity type)`` pairs found in one text.
    spans: ClassVar[list[tuple[str, str]]] = [
        ("alice@example.com", "EMAIL"),
        ("+1 555 0100", "PHONE"),
        ("acme", "ORGANIZATION"),
    ]

    def _decide(self) -> tuple[Detection, Detection]:
        text = " and ".join(value for value, _ in self.spans)
        detection = Detection()
        for value, entity in self.spans:
            start = text.index(value)
            detection.add(start, start + len(value), entity, value)

        async def decide() -> Detection:
            policy = await self.opened()
            kept: Detection = await policy.decide(Detection(list(detection.spans)), Context())
            return kept

        return detection, run(decide)

    def test_no_span_is_added(self) -> None:
        detection, kept = self._decide()
        assert all(span in detection.spans for span in kept.spans)

    def test_decision_is_deterministic(self) -> None:
        assert self._decide()[1].spans == self._decide()[1].spans


class OperatorSuite(ComponentSuite):
    """An operator: restores what it replaced, leaves unknown text, keeps its fixed text."""

    #: A text and the values in it the operator replaces, with their entity types.
    text: ClassVar[str] = "mail alice@example.com or bob@example.org"
    values: ClassVar[list[tuple[str, str]]] = [
        ("alice@example.com", "EMAIL"),
        ("bob@example.org", "EMAIL"),
    ]

    def _detection(self) -> Detection:
        detection = Detection()
        for value, entity in self.values:
            start = self.text.index(value)
            detection.add(start, start + len(value), entity, value)
        return detection

    def test_pseudonymize_then_deanonymize_restores_the_text(self) -> None:
        async def check() -> None:
            operator = await self.opened()
            session = Session()
            masked = await operator.pseudonymize(
                self.text, self._detection(), session, Context()
            )
            assert all(value not in masked.text for value, _ in self.values)
            if operator.fixed_text:
                return  # not reversible
            restored = await operator.deanonymize(masked.text, session, Context())
            assert restored.text == self.text

        run(check)

    def test_unknown_text_is_left_alone(self) -> None:
        async def check() -> None:
            operator = await self.opened()
            text = "see <PRIVYX_EMAIL_DEADBEEFDEADBEEF> and user-0@example.invalid"
            restored = await operator.deanonymize(text, Session(), Context())
            assert restored.text == text

        run(check)

    def test_fixed_text_is_what_every_value_becomes(self) -> None:
        async def check() -> None:
            operator = await self.opened()
            if not operator.fixed_text:
                return
            masked = await operator.pseudonymize(
                self.text, self._detection(), Session(), Context()
            )
            assert all(t.replacement == operator.fixed_text for t in masked.transformations)

        run(check)


class AnchorSuite(ComponentSuite):
    """An anchor: one result for one key and value, another for another key."""

    #: Options with another key than :attr:`options`.
    other_options: ClassVar[dict[str, Any]] = {}
    #: What :attr:`~privyx.anchors.Anchor.shared` is with :attr:`options`.
    shared: ClassVar[bool] = True

    def _anchor(self, value: str, **options: Any) -> str:
        async def anchor() -> str:
            component = await self.opened(**options)
            result: str = await component.anchor(value, Context())
            return result

        return run(anchor)

    def test_the_same_key_gives_the_same_result(self) -> None:
        assert self._anchor("alice@example.com") == self._anchor("alice@example.com")
        assert self._anchor("alice@example.com") != self._anchor("bob@example.com")

    def test_another_key_gives_another_result(self) -> None:
        if not self.other_options:
            return
        other = self._anchor("alice@example.com", **self.other_options)
        assert self._anchor("alice@example.com") != other

    def test_shared_follows_the_options(self) -> None:
        assert self.create().shared is self.shared


class VaultSuite(ComponentSuite):
    """A vault: create, read, save, delete; TTL, isolation, concurrency, listing, its lock."""

    def _with_vault(self, check: Callable[[Any], Awaitable[None]], **options: Any) -> None:
        async def main() -> None:
            vault = await self.opened(**options)
            try:
                await check(vault)
            finally:
                await vault.close()

        run(lambda: main())

    def test_create_get_save_delete(self) -> None:
        async def check(vault: Any) -> None:
            session = Session()
            session.put("<PRIVYX_EMAIL_1>", "alice@example.com")
            await vault.create(session)
            loaded = await vault.get(session.session_id)
            assert loaded is not None and loaded.get("<PRIVYX_EMAIL_1>") == "alice@example.com"
            session.put("<PRIVYX_EMAIL_2>", "bob@example.com")
            await vault.save(session)
            loaded = await vault.get(session.session_id)
            assert loaded is not None and loaded.get("<PRIVYX_EMAIL_2>") == "bob@example.com"
            await vault.delete(session.session_id)
            assert await vault.get(session.session_id) is None
            await vault.delete(session.session_id)  # deleting again is no error

        self._with_vault(check)

    def test_a_session_is_created_once(self) -> None:
        async def check(vault: Any) -> None:
            session = Session()
            results = await asyncio.gather(
                *(vault.create(Session(session_id=session.session_id)) for _ in range(5)),
                return_exceptions=True,
            )
            assert sum(result is None for result in results) == 1
            assert all(isinstance(r, VaultError) for r in results if r is not None)

        self._with_vault(check)

    def test_sessions_are_isolated(self) -> None:
        async def check(vault: Any) -> None:
            first, second = Session(), Session()
            first.put("<PRIVYX_PERSON_1>", "alice")
            second.put("<PRIVYX_PERSON_1>", "bob")
            await asyncio.gather(vault.create(first), vault.create(second))
            one, two = await vault.get(first.session_id), await vault.get(second.session_id)
            assert one is not None and one.get("<PRIVYX_PERSON_1>") == "alice"
            assert two is not None and two.get("<PRIVYX_PERSON_1>") == "bob"

        self._with_vault(check)

    def test_ttl_hides_an_idle_session(self) -> None:
        """With a ``ttl`` counted from a session's last use; a store that counts it
        from the last write overrides this check."""
        if "ttl" not in self.component.Options.model_fields:
            return

        async def check(vault: Any) -> None:
            idle, live = Session(), Session()
            for session, age in ((idle, 120), (live, 1)):
                await vault.create(session)
                session.updated_at -= age
                await vault.save(session)
            assert await vault.get(idle.session_id) is None
            assert await vault.get(live.session_id) is not None

        self._with_vault(check, ttl=60)

    def test_list_sessions_when_supported(self) -> None:
        async def check(vault: Any) -> None:
            session = Session()
            await vault.create(session)
            try:
                listed = await vault.list_sessions()
            except VaultError:
                return  # optional
            assert session.session_id in {s.session_id for s in listed}

        self._with_vault(check)

    def test_a_released_lock_no_longer_saves(self) -> None:
        async def check(vault: Any) -> None:
            session = Session()
            await vault.create(session)
            held = vault.lock(session.session_id)
            if held is None:
                return  # this process's alone
            async with held as token:
                await vault.save(session, lock=token)
            try:
                await vault.save(session, lock=token)
            except VaultError:
                return
            raise AssertionError("save took a lock token after the lock was released")

        self._with_vault(check)

    def test_a_record_reads_back_as_written(self) -> None:
        from privyx.vaults import StoredVault

        if not issubclass(self.component, StoredVault):
            return

        async def check(vault: Any) -> None:
            now = time.time()
            assert await vault.write("privyx-suite", b'{"a": 1}', updated_at=now)
            assert await vault.read("privyx-suite") == b'{"a": 1}'
            assert not await vault.write("privyx-suite", b"{}", updated_at=now, new=True)
            await vault.remove("privyx-suite")
            assert await vault.read("privyx-suite") is None

        self._with_vault(check)


class ProviderSuite(ComponentSuite):
    """A provider: requests stay on ``base_url``'s origin, and carry its credential."""

    #: Client paths the provider is asked for.
    paths: ClassVar[list[str]] = ["/v1/chat/completions", "/v1/embeddings", "/v1/messages"]

    def test_urls_stay_on_the_origin_of_base_url(self) -> None:
        provider = self.create()
        base = urlsplit(provider.options.base_url or provider.url("/", stream=False))
        for path in self.paths:
            for stream in (False, True):
                url = urlsplit(provider.url(path, stream=stream))
                assert url.scheme in ("http", "https"), url
                assert (url.scheme, url.netloc) == (base.scheme, base.netloc), (path, url)

    def test_the_credential_is_sent(self) -> None:
        import httpx

        provider = self.create()
        if not provider.options.api_key:
            return
        sent: list[httpx.Request] = []

        def reply(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={})

        async def send() -> None:
            transport = httpx.MockTransport(reply)
            async with httpx.AsyncClient(transport=transport) as client:
                await client.post(
                    provider.url(self.paths[0], stream=False),
                    json={},
                    headers=provider.headers(),
                    auth=provider.auth() or httpx.USE_CLIENT_DEFAULT,
                )

        run(send)
        values = " ".join(sent[0].headers.values())
        assert provider.options.api_key in values


class SecretResolverSuite(ComponentSuite):
    """A secret resolver: the secret its reference names, and errors that never show it."""

    #: A reference the resolver reads, and the secret it names.
    reference: ClassVar[str]
    secret: ClassVar[str]
    #: A reference it cannot read.
    broken: ClassVar[str]

    def test_the_reference_resolves(self) -> None:
        assert self.create().resolve(self.reference, "provider.api_key") == self.secret

    def test_a_broken_reference_is_an_error_naming_the_setting(self) -> None:
        from privyx.core import ConfigError

        try:
            self.create().resolve(self.broken, "provider.api_key")
        except ConfigError as exc:
            assert "provider.api_key" in str(exc)
            assert self.secret not in str(exc)
            return
        raise AssertionError("a broken reference resolved")


class AgentSuite(ComponentSuite):
    """An agent type: argv without a shell, the proxy URL kept, an explicit upstream kept.

    The plan is made with :attr:`environment` as the tool's, over the test
    process's own: name a home with no settings in it, so the tool's own
    configuration on the machine is not read.
    """

    #: Variables of the tool's environment while the plan is made.
    environment: ClassVar[dict[str, str]] = {}

    def plan(self, **settings: Any) -> Any:
        """The plan of launching the type with ``settings``, as ``privyx run`` makes it."""
        import os
        from unittest import mock

        from privyx.agents import Target, build_launch_plan
        from privyx.config.schema import Settings

        def upstream(settings: Settings) -> tuple[str, str]:
            url = settings.upstream_url or "https://provider.example.com"
            return url, url

        spec = Target(
            command="privyx-suite-agent",
            provider="openai",
            env_vars=("PRIVYX_SUITE_BASE_URL",),
            agent=self.create(),
        )
        with mock.patch.dict(os.environ, self.environment):
            return build_launch_plan(
                spec,
                ["--privyx-suite"],
                Settings(**settings),
                43210,
                spec.env_vars,
                upstream=upstream,
            )

    def test_argv_is_strings_with_the_users_arguments(self) -> None:
        argv = self.plan().argv
        assert all(isinstance(arg, str) for arg in argv)
        assert "--privyx-suite" in argv

    def test_env_vars_get_the_proxy_url(self) -> None:
        plan = self.plan()
        assert plan.environment["PRIVYX_SUITE_BASE_URL"] == plan.base_url

    def test_an_explicit_upstream_wins_over_the_tools_endpoint(self) -> None:
        plan = self.plan(upstream_url="https://upstream.example.com")
        assert plan.upstream == "https://upstream.example.com"


#: The value a dialect's suite plants in every string of a request.
_PLANTED = "alice@example.com"


class DialectSuite(ComponentSuite):
    """A dialect: each string of its requests masked but the opaque ones, its replies restored.

    The suite plants a fake email in every string of :attr:`requests` but
    the opaque ones and those of :attr:`config_keys`, and in a key no format
    has in each object there; masked, none may be left, so a key the dialect
    does not walk shows.  It also checks
    that a request restores, that the leak check finds a copy outside the
    content, and that :attr:`stream`, a reply streamed in the format, is
    restored however it is split.
    """

    #: Requests as a client of the format sends them.
    requests: ClassVar[list[dict[str, Any]]]
    #: Their top-level keys that hold settings, not text, such as ``model``.
    config_keys: ClassVar[frozenset[str]] = frozenset({"model"})
    #: A streamed reply (SSE, or JSON lines when the format streams them) in
    #: which ``{token}`` stands for a token Privyx minted; ``""`` for none.
    stream: ClassVar[str] = ""

    def _engine(self) -> Any:
        from privyx.detectors import RegexDetector
        from privyx.engine import PrivacyEngine
        from privyx.operators import PseudonymOperator
        from privyx.policies import DefaultPolicy
        from privyx.vaults import MemoryVault

        return PrivacyEngine(
            detector=RegexDetector(),
            policy=DefaultPolicy(),
            operator=PseudonymOperator(),
            vault=MemoryVault(),
            dlp_check=True,
        )

    def _planted(self, request: dict[str, Any]) -> dict[str, Any]:
        dialect = self.create()
        out: dict[str, Any] = {}
        for key, value in request.items():
            if key in dialect.wrapped_keys and isinstance(value, dict):
                out[key] = self._planted(value)  # a request of its own
            elif key in self.config_keys:
                out[key] = value
            else:
                out[key] = _plant(value, dialect.opaque_keys)
        return out

    def _mask(self, request: dict[str, Any], **extra: Any) -> tuple[Any, Any, Any]:
        from privyx.proxy import transform_request

        dialect = self.create()
        engine = self._engine()
        session = engine.ephemeral_session()

        async def mask() -> Any:
            return await transform_request(
                request, engine, session.session_id, session=session, dialect=dialect, **extra
            )

        return run(mask), engine, session

    def test_every_string_in_the_content_is_masked(self) -> None:
        from privyx.core import LeakError, opaque

        dialect = self.create()
        for request in self.requests:
            try:
                masked, _, _ = self._mask(self._planted(request))
            except LeakError:
                raise AssertionError("a planted value was left in the request") from None
            for key, value in masked.items():
                for path, text in _leaves(value, dialect.opaque_keys, opaque):
                    assert _PLANTED not in text, f"{key}{path} was not masked"

    def test_a_masked_request_restores(self) -> None:
        from privyx.core import walk_sync

        for request in self.requests:
            planted = self._planted(request)
            masked, engine, session = self._mask(planted)

            def restore(text: str, mapping: dict[str, str] = session.mapping) -> str:
                for token, value in mapping.items():
                    text = text.replace(token, value)
                return text

            assert walk_sync(masked, restore) == planted

    def test_the_leak_check_finds_a_copy_outside_the_content(self) -> None:
        from privyx.core import LeakError

        request = {**self._planted(self.requests[0]), "privyx_suite_config": _PLANTED}
        try:
            self._mask(request)
        except LeakError:
            return
        raise AssertionError("a copy outside the content was forwarded")

    def test_a_stream_is_restored_however_it_is_split(self) -> None:
        if not self.stream:
            return
        from privyx.streaming import LineRouter, TokenStreamProcessor
        from privyx.token import FormatCodec

        codec = FormatCodec.default()
        token = "<PRIVYX_EMAIL_0123456789ABCDEF>"
        mapping = {token: _PLANTED}
        raw = self.stream.replace("{token}", token).encode()
        dialect = self.create()

        def make() -> Any:
            return TokenStreamProcessor(codec, mapping.get)

        for cut in range(1, len(raw)):
            if dialect.line_stream:
                full = make()
                objects = dialect.objects(make, lambda text, f=full: f.feed(text) + f.flush())
                router: Any = LineRouter(objects)
            else:
                router = dialect.stream(codec, mapping.get, make)
            out = "".join([*router.feed(raw[:cut]), *router.feed(raw[cut:]), *router.flush()])
            assert token not in out and _PLANTED in out, f"split at byte {cut}"


class FeatureSuite(ComponentSuite):
    """A feature: its listeners come and go with it, and the exchange stays private.

    The checks switch it on alone in an app, as a setting would, and send
    :attr:`request`, a value planted in it, through a pipeline to an upstream
    that echoes it back.  A feature that refuses such a request on purpose
    overrides :meth:`test_a_request_goes_out_masked_and_notifications_hold_no_value`.
    """

    component: ClassVar[type[Feature]]
    #: A chat request (OpenAI's format) the feature lets through.
    request: ClassVar[dict[str, Any]] = {
        "model": "privyx-suite",
        "messages": [{"role": "user", "content": f"write to {_PLANTED}"}],
    }

    def test_switching_it_on_adds_its_listeners_and_off_removes_them(self) -> None:
        async def check() -> None:
            app = self._app()
            name = self.component.name
            marked = listened(self.component)
            async with self._on(app):
                feature = app.features.get(name)
                assert isinstance(feature, self.component)
                for event, _, _ in marked:
                    assert _owned(app._events.listeners(event), feature), event.name
            assert app.features.get(name) is None
            for event, _, _ in marked:
                assert not _owned(app._events.listeners(event), feature), event.name

        run(check)

    def test_a_request_goes_out_masked_and_notifications_hold_no_value(self) -> None:
        import json

        import httpx

        from privyx.core import Exchange, Notification
        from privyx.detectors import RegexDetector
        from privyx.engine import PrivacyEngine
        from privyx.operators import PseudonymOperator
        from privyx.policies import DefaultPolicy
        from privyx.proxy import Pipeline
        from privyx.vaults import MemoryVault

        sent: list[bytes] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            sent.append(request.content)
            echoed = json.loads(request.content)["messages"][-1]
            return httpx.Response(200, json={"choices": [{"message": echoed}]})

        async def check() -> tuple[int, list[Event]]:
            app = self._app()
            with capture_events(app, Notification) as heard:
                async with self._on(app) as audit:
                    engine = PrivacyEngine(
                        detector=RegexDetector(),
                        policy=DefaultPolicy(),
                        operator=PseudonymOperator(),
                        vault=MemoryVault(),
                        audit=audit,
                        dlp_check=True,
                    )
                    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                    pipeline = Pipeline(
                        engine,
                        origin="https://upstream.example.com",
                        client=client,
                        audit=audit,
                        events=app._events,
                    )
                    body = json.dumps(self.request).encode()
                    headers = {"content-type": "application/json"}
                    reply = await pipeline.handle(
                        Exchange("POST", "/v1/chat/completions", headers, body)
                    )
                    await pipeline.close()
            return reply.status_code, heard

        status, heard = run(check)
        assert status == 200, f"the request was answered with {status}"
        assert sent, "the request never reached the upstream"
        assert all(_PLANTED.encode() not in body for body in sent)
        for note in heard:
            assert _PLANTED not in repr(note), f"{note.name} holds the planted value"

    def _app(self) -> Any:
        """An app with :attr:`component` registered, reading no config file."""
        from privyx.app import Privyx

        app = Privyx()
        registry = app.registry(Feature)
        if not registry.is_builtin(self.component.name):
            registry.add(self.component)
        return app

    def _on(self, app: Any) -> Any:
        """``app`` with the feature alone switched on, with :attr:`options`."""
        from privyx.config.schema import Settings

        name = self.component.name
        return app.features.running(Settings(), only={name}, extra={name: self.options})


def _owned(listeners: tuple[Any, ...], feature: Any) -> bool:
    """Whether one of ``listeners`` is a method of ``feature``."""
    return any(getattr(listener, "__self__", None) is feature for listener in listeners)


def _plant(value: Any, hidden: frozenset[str]) -> Any:
    """``value`` with each string but the opaque ones the planted email, and in each
    object an unknown key holding it."""
    from privyx.core import opaque

    if isinstance(value, str):
        return _PLANTED
    if isinstance(value, list):
        return [_plant(item, hidden) for item in value]
    if isinstance(value, dict):
        planted = {
            k: v if opaque(k, value, hidden) else _plant(v, hidden) for k, v in value.items()
        }
        return {**planted, "privyx_suite_field": _PLANTED}
    return value


def _leaves(value: Any, hidden: frozenset[str], opaque: Any, path: str = "") -> Any:
    """Each string of ``value`` not under an opaque key, with its path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _leaves(item, hidden, opaque, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not opaque(key, value, hidden):
                yield from _leaves(item, hidden, opaque, f"{path}.{key}")
