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
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import ValidationError

from privyx.core import Component, Context, Detection, EventBus, Services, Session, VaultError
from privyx.token import FormatCodec


def services(**changes: Any) -> Services:
    """What an app gives a component in ``open()``: the default codec, no anchor."""
    values: dict[str, Any] = {"codec": FormatCodec.default(), "anchor": None, "events": EventBus()}
    return Services(**{**values, **changes})


async def opened[C: Component](component: C, **changes: Any) -> C:
    """``component``, opened with :func:`services` and ``changes`` to them."""
    await component.open(services(**changes))
    return component


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
