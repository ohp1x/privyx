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
    from privyx.core.result import Detection
    from privyx.privacy.detector.base import Detector
    from privyx.privacy.policy.base import Policy

Check = Callable[["Settings"], tuple[bool, str]]


@click.command()
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
def doctor(config_path: str | None) -> None:
    """Run diagnostic checks system-wide."""
    from privyx.config.loader import load_config

    click.echo("Privyx Doctor")
    click.echo("=============")
    try:
        settings = load_config(config_path)
    except Exception as exc:
        click.echo(f"  ✗ configuration: {type(exc).__name__}: {exc}")
        sys.exit(1)

    checks: list[tuple[str, Check]] = [
        # First: loads plugins into the registry so the component checks below
        # can see plugin-provided types.
        ("plugins", _check_plugins),
        ("detector", _check_detector),
        ("permissions", _check_permissions),
        ("vault", _check_vault),
        ("provider", _check_provider),
        ("proxy", _check_proxy),
        ("streaming", _check_streaming),
        ("routes", _check_routes),
        ("configuration", _check_configuration),
    ]
    all_ok = True
    for name, check in checks:
        try:
            ok, msg = check(settings)
        except Exception as exc:  # a check that blew up is a failed check
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        status = "✓" if ok else "✗"
        click.echo(f"  {status} {name}: {msg}")
        all_ok = all_ok and ok
    if not all_ok:
        sys.exit(1)


def _check_plugins(settings: Settings) -> tuple[bool, str]:
    """Configured plugins load, and report what each family gained.

    Runs first so the detector/vault/provider checks below resolve plugin types.
    """
    from privyx.plugins.loader import load_plugins
    from privyx.plugins.registry import PLUGINS

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


def _check_detector(settings: Settings) -> tuple[bool, str]:
    """The configured detector compiles and scans a diagnostic sample."""
    from privyx.core.builder import build_detector_from

    detector = build_detector_from(settings)
    _, result = asyncio.run(_probe_sample(settings, detector))
    if not result.spans:
        sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
        if any(
            section.type == "regex"
            and not any(
                "EMAIL" in rules
                for rules in (section.disable, section.patterns, section.terms, section.context)
            )
            for section in sections
        ):
            return (False, "configured detector missed the built-in email sample")
        return (True, "skipped: no diagnostic sample matched the configured detector")
    types = sorted({span.entity_type for span in result.spans})
    return (True, f"{settings.detector_type}: {len(result.spans)} span(s) {types}")


async def _probe_sample(
    settings: Settings, detector: Detector, policy: Policy | None = None
) -> tuple[str, Detection]:
    """Find a covered sample without assuming that every setup masks email.

    Try a synthetic email, then configured terms only for regex/yaml pipelines.
    Other detectors may send their input elsewhere. Callers must report a lack
    of matching samples explicitly rather than claiming coverage. Sample values
    are never included in diagnostic output.
    """
    from privyx.core.context import Context
    from privyx.core.result import Detection

    samples = ["mail alice@example.com"]
    sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    # Composite detectors send every candidate to every member.
    if all(section.type in {"regex", "yaml"} for section in sections):
        for section in sections:
            for terms in section.terms.values():
                term = next((value for value in terms if value.strip()), "")
                if term:
                    samples.append(term)
    for text in samples:
        context = Context()
        detection = await detector.detect(text, context)
        if policy is not None:
            detection = await policy.decide(detection, context)
        if detection.spans:
            return text, detection.merged(text)
    return "", Detection()


def _check_permissions(settings: Settings) -> tuple[bool, str]:
    """Sensitive files (.db, audit log, anchor.key) must be owner-only (0600)."""
    import os
    from pathlib import Path

    if os.name != "posix":
        return (True, "skipped: non-posix platform")

    from privyx.core.builder import _sqlite_path
    from privyx.security.keys import default_anchor_secret_path

    candidates: list[Path] = [
        default_anchor_secret_path(),
        Path("anchor.key"),
        Path("privyx-audit.log"),
        Path("privyx.db"),
        Path("privyx.db-wal"),
        Path("privyx.db-shm"),
    ]
    if settings.vault.type == "sqlite":
        db_path = _sqlite_path(settings.vault.dsn)
        if db_path != ":memory:":
            candidates.extend([Path(db_path), Path(f"{db_path}-wal"), Path(f"{db_path}-shm")])
    if settings.audit.path:
        candidates.append(Path(os.path.expanduser(settings.audit.path)))

    seen: set[Path] = set()
    insecure: list[str] = []
    checked = 0

    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)

        try:
            if not path.is_file():
                continue
            mode = path.stat().st_mode
        except OSError:
            continue

        checked += 1
        if mode & 0o077 != 0:
            insecure.append(f"{path} ({oct(mode & 0o777)}): run 'chmod 600 {path}'")

    if insecure:
        return (False, f"insecure file permissions: {'; '.join(insecure)}")
    if checked:
        return (True, f"{checked} sensitive file(s) owner-only (0600)")
    return (True, "sensitive files owner-only (0600)")


def _check_vault(settings: Settings) -> tuple[bool, str]:
    """The configured vault round-trips a session.

    This is where a bad DSN, a missing driver or an unreachable Redis shows up
    — all of which otherwise surface only at the first request.
    """
    from privyx.core.builder import build_vault
    from privyx.core.session import Session

    async def _probe() -> tuple[bool, str]:
        vault, close = await build_vault(settings)
        session = Session()
        try:
            # An arbitrary mapping entry: the vault stores opaque key→value pairs
            # and neither knows nor cares about token syntax.
            session.put("doctor-probe-key", "probe")
            await vault.create(session)
            await vault.save(session)
            loaded = await vault.get(session.session_id)
            if loaded is None or loaded.get("doctor-probe-key") != "probe":
                return (False, f"{settings.vault.type}: round-trip lost the mapping")
            return (True, f"{settings.vault.type}: round-trip ok")
        finally:
            try:
                await vault.delete(session.session_id)
            finally:
                await close()

    return asyncio.run(_probe())


def _check_provider(settings: Settings) -> tuple[bool, str]:
    """The configured provider is registered and constructible."""
    if importlib.util.find_spec("httpx") is None:
        return (False, "httpx not installed")

    from privyx.config.redact import redact
    from privyx.plugins.registry import PLUGINS
    from privyx.providers.registry import build_provider, default_registry, resolve_base_url

    ptype = settings.provider.type
    known = default_registry().names()
    if ptype not in known and ptype not in PLUGINS.providers:
        available = known + [f"{n} (plugin)" for n in PLUGINS.providers.names()]
        return (False, f"unknown provider {ptype!r} (known: {', '.join(available)})")
    build_provider(settings)
    return (True, f"{ptype} → {redact(resolve_base_url(settings), 'url')}")


def _check_proxy(settings: Settings) -> tuple[bool, str]:
    """A request payload survives the proxy with no PII left in it."""
    from privyx.core.builder import build_engine
    from privyx.core.session import Session
    from privyx.proxy.http import HTTPProxy
    from privyx.streaming.adapters.registry import build_stream_adapter

    async def _probe() -> tuple[bool, str]:
        engine, close = await build_engine(settings)
        session = Session()
        try:
            await engine.vault.create(session)
            text, detection = await _probe_sample(settings, engine.detector, engine.policy)
            if not detection.spans:
                return (True, "skipped: no diagnostic sample is masked by the detector and policy")
            proxy = HTTPProxy(
                engine=engine,
                provider=_ReplayProvider([]),
                stream_adapter=build_stream_adapter(settings.provider.type),
            )
            payload, session_id = await proxy.process_request(
                {"messages": [{"role": "user", "content": text}]}, session.session_id
            )
            stored = await engine.vault.get(session_id or "")
            transformed = payload["messages"][0]["content"]
            remainder = transformed
            if stored:
                # A token's entity name can itself be a configured term.
                for replacement in sorted(stored.mapping, key=len, reverse=True):
                    remainder = remainder.replace(replacement, "")
            if settings.operator.type == "redact":
                remainder = remainder.replace(settings.operator.token, "")
            if transformed == text or any(span.text in remainder for span in detection.spans):
                return (False, "diagnostic sample survived the request transform")
            count = len(stored.mapping) if stored else 0
            return (True, f"request transformed, {count} mapping(s) vaulted")
        finally:
            try:
                await engine.vault.delete(session.session_id)
            finally:
                await close()

    return asyncio.run(_probe())


def _check_streaming(settings: Settings) -> tuple[bool, str]:
    """A pseudonym split across SSE chunks is restored before the client sees it.

    The split lands inside the pseudonym on purpose: that is exactly the case a
    stateless SSE parser gets wrong.
    """
    import json

    from privyx.core.builder import build_engine
    from privyx.core.session import Session
    from privyx.proxy.http import HTTPProxy
    from privyx.proxy.stream_router import resolver_for
    from privyx.streaming.adapters.openai import OpenAIStreamAdapter
    from privyx.streaming.sse import parse_sse

    async def _probe() -> tuple[bool, str]:
        engine, close = await build_engine(settings)
        session = Session()
        try:
            await engine.vault.create(session)
            text, detection = await _probe_sample(settings, engine.detector, engine.policy)
            if not detection.spans:
                return (True, "skipped: no diagnostic sample is masked by the detector and policy")
            result = await engine.transform(text, session=session)
            if not result.transformations or result.text == text:
                return (False, "diagnostic sample was not transformed for streaming")
            pseudonym = result.transformations[0].replacement
            original = result.transformations[0].original
            # Reversibility is "can the streaming resolver recover the original?",
            # not "is the plaintext stored verbatim" — encrypt stores ciphertext
            # and decrypts through its resolver.
            resolve = resolver_for(engine.operator, session.mapping)
            if resolve(pseudonym) != original:
                return (True, f"skipped: operator {settings.operator.type!r} is not reversible")

            payload = {"choices": [{"delta": {"content": f"to {pseudonym} ok"}}]}
            body = f"data: {json.dumps(payload)}\n\n"
            encoded = json.dumps(pseudonym)[1:-1]
            midpoint = body.index(encoded) + len(encoded) // 2
            adapter = OpenAIStreamAdapter()
            proxy = HTTPProxy(
                engine=engine,
                provider=_ReplayProvider([body[:midpoint], body[midpoint:]]),
                stream_adapter=adapter,
            )
            out = "".join([c async for c in proxy.process_stream({}, session.session_id)])
            restored = "".join(adapter.extract_delta(event) for event in parse_sse(out))
            if pseudonym in restored:
                return (False, "pseudonym leaked through the stream")
            if restored != f"to {original} ok":
                return (False, "stream did not restore the original value")
            return (True, "pseudonym restored across a split chunk")
        finally:
            try:
                await engine.vault.delete(session.session_id)
            finally:
                await close()

    return asyncio.run(_probe())


def _check_routes(settings: Settings) -> tuple[bool, str]:
    """Describe effective masking coverage and expose unmasked forwarding."""
    from privyx.config.schema import ProxyConfig
    from privyx.proxy.schemas import KNOWN_SCHEMAS

    proxy = settings.proxy
    invalid = [
        path
        for path, schema in proxy.routes.items()
        if not path.startswith("/")
        or path.startswith("//")
        or any(c.isspace() or c in "?#\x00" for c in path)
        or schema not in KNOWN_SCHEMAS
    ]
    if invalid:
        return False, "invalid path or unsupported schema in proxy.routes: " + ", ".join(invalid)
    if not proxy.routes:
        return False, "no masking routes configured"

    routes = proxy.routes
    if proxy.mode == "gateway":
        from privyx.gateway.server import served_routes

        routes = served_routes(routes, google=settings.provider.type == "google")
        disposition = "unlisted paths return 404; auxiliary routes are not served"
    elif proxy.passthrough_unknown:
        disposition = (
            "WARNING: unlisted paths pass through WITHOUT body masking; "
            "set proxy.passthrough_unknown: false to return 403"
        )
    else:
        disposition = "unlisted paths are blocked (403)"
    if not routes:
        return False, "no masking routes served in " + proxy.mode + " mode"
    missing = sorted(ProxyConfig().routes.keys() - routes.keys())
    coverage = ", ".join(f"{p} ({s})" for p, s in sorted(routes.items())) or "none"
    return True, (
        f"{proxy.mode}: {len(routes)} masking route(s): {coverage}; {disposition}; "
        f"default paths outside coverage: {', '.join(missing) or 'none'}"
    )


def _check_configuration(settings: Settings) -> tuple[bool, str]:
    """Settings load and validate, and every named component can be built.

    ``build_anchor`` is called here rather than getting a check of its own: an
    unknown ``anchor.type`` is a config error, and this is the check that
    reports config errors.  It also makes the anchor state visible, since
    "anchored or not" changes what pseudonyms look like.
    """
    from privyx.core.builder import build_anchor

    anchor = build_anchor(settings)  # raises ConfigError on an unknown type
    anchoring = f"anchor={settings.anchor.type}" if anchor else "anchor=off"
    return (
        True,
        f"valid: detector={settings.detector_type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type} {anchoring}",
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
