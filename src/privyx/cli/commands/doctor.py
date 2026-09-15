"""`privyx doctor` command — run diagnostic checks.

Each check exercises the *configured* component rather than asserting a static
truth: the vault check writes and reads back a real session through the
configured backend, and the proxy/streaming checks push a pseudonym through
:meth:`HTTPProxy.process_stream` split at an awkward boundary.  A check that
cannot fail is not a check.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from privyx.config.schema import Settings

Check = Callable[[str | None], tuple[bool, str]]


@click.command()
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
def doctor(config_path: str | None) -> None:
    """Run diagnostic checks system-wide."""
    click.echo("Privyx Doctor")
    click.echo("=============")
    checks: list[tuple[str, Check]] = [
        # First: loads plugins into the registry so the component checks below
        # can see plugin-provided types.
        ("plugins", _check_plugins),
        ("detector", _check_detector),
        ("vault", _check_vault),
        ("provider", _check_provider),
        ("proxy", _check_proxy),
        ("streaming", _check_streaming),
        ("configuration", _check_configuration),
    ]
    all_ok = True
    for name, check in checks:
        try:
            ok, msg = check(config_path)
        except Exception as exc:  # a check that blew up is a failed check
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        status = "✓" if ok else "✗"
        click.echo(f"  {status} {name}: {msg}")
        all_ok = all_ok and ok
    if not all_ok:
        sys.exit(1)


def _settings(config_path: str | None) -> Settings:
    from privyx.config.loader import load_config

    return load_config(config_path)


def _check_plugins(config_path: str | None) -> tuple[bool, str]:
    """Configured plugins load, and report what each family gained.

    Runs first so the detector/vault/provider checks below resolve plugin types.
    """
    from privyx.plugins.loader import load_plugins
    from privyx.plugins.registry import PLUGINS

    settings = _settings(config_path)
    if not settings.plugins.enabled or not settings.plugins.paths:
        return (True, "no plugin paths configured")

    load_plugins(settings)  # raises ConfigError on a bad path/module/collision
    families = {
        "detector": PLUGINS.detectors.names(),
        "operator": PLUGINS.operators.names(),
        "policy": PLUGINS.policies.names(),
        "provider": PLUGINS.providers.names(),
        "anchor": PLUGINS.anchors.names(),
        "vault": PLUGINS.vaults.names(),
    }
    total = sum(len(names) for names in families.values())
    if not total:
        return (True, f"paths {settings.plugins.paths} loaded no components")
    summary = ", ".join(f"{fam}: {', '.join(names)}" for fam, names in families.items() if names)
    return (True, f"{total} plugin(s) — {summary}")


def _check_detector(config_path: str | None) -> tuple[bool, str]:
    """The configured detector compiles and finds a known entity."""
    from privyx.core.builder import build_detector_from
    from privyx.core.context import Context

    settings = _settings(config_path)
    detector = build_detector_from(settings)
    result = asyncio.run(detector.detect("mail alice@example.com", Context()))
    if not result.spans:
        return (False, "configured detector found nothing in a known-PII sample")
    types = sorted({span.entity_type for span in result.spans})
    return (True, f"{settings.detector.type}: {len(result.spans)} span(s) {types}")


def _check_vault(config_path: str | None) -> tuple[bool, str]:
    """The configured vault round-trips a session.

    This is where a bad DSN, a missing driver or an unreachable Redis shows up
    — all of which otherwise surface only at the first request.
    """
    from privyx.core.builder import build_vault
    from privyx.core.session import Session

    settings = _settings(config_path)

    async def _probe() -> tuple[bool, str]:
        vault, close = await build_vault(settings)
        try:
            session = Session()
            # An arbitrary mapping entry: the vault stores opaque key→value pairs
            # and neither knows nor cares about token syntax.
            session.put("doctor-probe-key", "probe")
            await vault.create(session)
            await vault.save(session)
            loaded = await vault.get(session.session_id)
            await vault.delete(session.session_id)
            if loaded is None or loaded.get("doctor-probe-key") != "probe":
                return (False, f"{settings.vault.type}: round-trip lost the mapping")
            return (True, f"{settings.vault.type}: round-trip ok")
        finally:
            await close()

    return asyncio.run(_probe())


def _check_provider(config_path: str | None) -> tuple[bool, str]:
    """The configured provider is registered and constructible."""
    if importlib.util.find_spec("httpx") is None:
        return (False, "httpx not installed")

    from privyx.plugins.registry import PLUGINS
    from privyx.providers.registry import build_provider, default_registry, resolve_base_url

    settings = _settings(config_path)
    ptype = settings.provider.type
    known = default_registry().names()
    if ptype not in known and ptype not in PLUGINS.providers:
        available = known + [f"{n} (plugin)" for n in PLUGINS.providers.names()]
        return (False, f"unknown provider {ptype!r} (known: {', '.join(available)})")
    build_provider(settings)
    return (True, f"{ptype} → {resolve_base_url(settings)}")


def _check_proxy(config_path: str | None) -> tuple[bool, str]:
    """A request payload survives the proxy with no PII left in it."""
    from privyx.core.builder import build_engine
    from privyx.proxy.http import HTTPProxy
    from privyx.streaming.adapters.registry import build_stream_adapter

    settings = _settings(config_path)

    async def _probe() -> tuple[bool, str]:
        engine, close = await build_engine(settings)
        try:
            proxy = HTTPProxy(
                engine=engine,
                provider=_ReplayProvider([]),
                stream_adapter=build_stream_adapter(settings.provider.type),
            )
            payload, session_id = await proxy.process_request(
                {"messages": [{"role": "user", "content": "contact alice@example.com"}]}
            )
            if "alice@example.com" in payload["messages"][0]["content"]:
                return (False, "PII survived the request transform")
            session = await engine.vault.get(session_id or "")
            count = len(session.mapping) if session else 0
            return (True, f"request transformed, {count} mapping(s) vaulted")
        finally:
            await close()

    return asyncio.run(_probe())


def _check_streaming(config_path: str | None) -> tuple[bool, str]:
    """A pseudonym split across SSE chunks is restored before the client sees it.

    The split lands inside the pseudonym on purpose: that is exactly the case a
    stateless SSE parser gets wrong.
    """
    import json

    from privyx.core.builder import build_engine
    from privyx.proxy.http import HTTPProxy
    from privyx.streaming.adapters.openai import OpenAIStreamAdapter

    settings = _settings(config_path)
    email = "alice@example.com"

    async def _probe() -> tuple[bool, str]:
        engine, close = await build_engine(settings)
        try:
            session = await engine.get_or_create_session()
            await engine.transform(f"mail {email}", session=session)
            if not session.mapping:
                return (True, "skipped: configured detector found no PII to stream")
            pseudonym = next(iter(session.mapping))
            if session.mapping[pseudonym] != email:
                return (True, f"skipped: operator {settings.operator.type!r} is not reversible")

            payload = {"choices": [{"delta": {"content": f"to {pseudonym} ok"}}]}
            body = f"data: {json.dumps(payload)}\n\n"
            midpoint = body.index(pseudonym) + len(pseudonym) // 2
            proxy = HTTPProxy(
                engine=engine,
                provider=_ReplayProvider([body[:midpoint], body[midpoint:]]),
                stream_adapter=OpenAIStreamAdapter(),
            )
            out = "".join([c async for c in proxy.process_stream({}, session.session_id)])
            if pseudonym in out:
                return (False, "pseudonym leaked through the stream")
            if email not in out:
                return (False, "stream did not restore the original value")
            return (True, "pseudonym restored across a split chunk")
        finally:
            await close()

    return asyncio.run(_probe())


def _check_configuration(config_path: str | None) -> tuple[bool, str]:
    """Settings load and validate, and every named component can be built.

    ``build_anchor`` is called here rather than getting a check of its own: an
    unknown ``anchor.type`` is a config error, and this is the check that
    reports config errors.  It also makes the anchor state visible, since
    "anchored or not" changes what pseudonyms look like.
    """
    from privyx.core.builder import build_anchor

    s = _settings(config_path)
    anchor = build_anchor(s)  # raises ConfigError on an unknown type
    anchoring = f"anchor={s.anchor.type}" if anchor else "anchor=off"
    return (
        True,
        f"valid: detector={s.detector.type} policy={s.policy.type} "
        f"operator={s.operator.type} vault={s.vault.type} {anchoring}",
    )


class _ReplayProvider:
    """Provider that replays fixed chunks, so no network is ever touched."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def send(self, payload: Any, session_id: str | None = None) -> Any:
        raise AssertionError("doctor must not contact the upstream provider")

    async def stream(
        self, payload: Any, session_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        return None
