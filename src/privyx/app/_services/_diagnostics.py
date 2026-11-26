"""``app.diagnostics``: the checks of ``privyx doctor`` and ``privyx config lint`` (experimental).

Each doctor check exercises the *configured* component rather than asserting a
static truth: the vault check writes and reads back a real session through the
configured backend, the proxy check masks a request as the proxy does, and the
streaming check pushes a pseudonym through the stream router split at an
awkward boundary.  A check that cannot fail is not a check.

Lint tries each term of the config in other spellings, and a few fake
built-in values inside identifiers, through the configured regex detectors
and policy.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from privyx.app import Privyx
    from privyx.config.schema import Settings
    from privyx.core import Detection, Session
    from privyx.detectors import Detector
    from privyx.engine import PrivacyEngine
    from privyx.policies import Policy

Check = Callable[["Settings", "Privyx"], tuple[bool, str]]


@dataclass(frozen=True)
class Checked:
    """One check of ``privyx doctor``: whether it passed, and what it found."""

    name: str
    ok: bool
    message: str


class Diagnostics:
    """The checks of one app's configuration."""

    def __init__(self, app: Privyx) -> None:
        self._app = app

    def doctor(self) -> list[Checked]:
        """Run each check of the configured components, in order.

        A configuration that cannot be read is the one failed check.
        """
        try:
            settings = self._app.settings
        except Exception as exc:
            return [Checked("configuration", False, f"{type(exc).__name__}: {exc}")]
        return _doctor(settings, self._app)

    async def lint(self) -> Lint:
        """The spellings of the configured terms, and built-in values, that are not masked.

        Raises:
            ConfigError: If the settings cannot be read.
        """
        return await _lint(self._app)


def _doctor(settings: Settings, app: Privyx) -> list[Checked]:
    auto = settings.anchor.model_dump().get("secret") == "auto"
    if auto:
        # A diagnosis must not create ~/.privyx/anchor.key: check with a key of its own.
        anchor = settings.anchor.model_copy(update={"secret": secrets.token_hex(32)})
        settings = settings.model_copy(update={"anchor": anchor})

    checks: list[tuple[str, Check]] = [
        # First: loads plugins into the app so the component checks below can
        # see plugin-provided types.
        ("plugins", _check_plugins),
        *([("anchor key", _check_anchor_key)] if auto else []),
        ("detector", _check_detector),
        ("permissions", _check_permissions),
        ("vault", _check_vault),
        ("provider", _check_provider),
        ("proxy", _check_proxy),
        ("streaming", _check_streaming),
        ("routes", _check_routes),
        ("configuration", _check_configuration),
    ]
    found = []
    for name, check in checks:
        try:
            ok, msg = check(settings, app)
        except Exception as exc:  # a check that blew up is a failed check
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        found.append(Checked(name, ok, msg))
    return found


def _check_plugins(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """Configured plugins load, and report what each kind gained.

    Runs first so the detector/vault/provider checks below resolve plugin types.
    """
    from privyx.app._plugins import load_plugins

    plugins = settings.plugins
    if not plugins.enabled or not (plugins.paths or plugins.modules):
        return (True, "no plugin paths configured")

    load_plugins(settings, app)  # raises ConfigError on a bad path/module/collision
    registries = app._registries
    families = {
        kind: [name for name in registries[kind].names() if not registries[kind].is_builtin(name)]
        for kind in ("detector", "operator", "policy", "provider", "anchor", "vault")
    }
    total = sum(len(names) for names in families.values())
    if not total:
        return (True, f"paths {settings.plugins.paths} loaded no components")
    summary = ", ".join(f"{fam}: {', '.join(names)}" for fam, names in families.items() if names)
    return (True, f"{total} plugin(s) — {summary}")


def _check_detector(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """The configured detector compiles and scans a diagnostic sample."""
    from privyx.app._build import build_detector

    detector = build_detector(settings.detectors, app)
    _, result = asyncio.run(_probe_sample(settings, app, detector))
    if not result.spans:
        sections = settings.active_detectors.values()
        if any(
            is_regex(app, section.type)
            and section.model_dump().get("builtins", True)
            and not any(
                "EMAIL" in (section.model_dump().get(rules) or ())
                for rules in ("disable", "patterns", "terms", "context")
            )
            for section in sections
        ):
            return (False, "configured detector missed the built-in email sample")
        return (True, "skipped: no diagnostic sample matched the configured detector")
    types = sorted({span.entity_type for span in result.spans})
    return (True, f"{settings.detector_type}: {len(result.spans)} span(s) {types}")


def is_regex(app: Privyx, kind: str) -> bool:
    """Whether detector type ``kind`` is the built-in ``regex`` one, whose options are known.

    Not a plugin extending it, which may send what it scans elsewhere.
    """
    from privyx.detectors import Detector, RegexDetector

    registry = app.registry(Detector)
    return kind in registry and registry.get(kind) is RegexDetector


async def _probe_sample(
    settings: Settings, app: Privyx, detector: Detector, policy: Policy | None = None
) -> tuple[str, Detection]:
    """Find a covered sample without assuming that every setup masks email.

    Try a synthetic email, then configured terms only for regex pipelines.
    Other detectors may send their input elsewhere. Callers must report a lack
    of matching samples explicitly rather than claiming coverage. Sample values
    are never included in diagnostic output.
    """
    from privyx.core import Context, Detection

    samples = ["mail alice@example.com"]
    sections = settings.active_detectors.values()
    # Composite detectors send every candidate to every member.
    if all(is_regex(app, section.type) for section in sections):
        for section in sections:
            for terms in section.model_dump().get("terms", {}).values():
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


@asynccontextmanager
async def _probing(settings: Settings, app: Privyx) -> AsyncIterator[tuple[PrivacyEngine, Session]]:
    """The configured engine and a session of its own, deleted however the check ends."""
    from privyx.app._build import build_engine
    from privyx.core import Session

    engine, close = await build_engine(settings, app)
    session = Session()
    try:
        await engine.vault.create(session)
        yield engine, session
    finally:
        try:
            await engine.vault.delete(session.session_id)
        finally:
            await close()


def _check_permissions(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """Sensitive files (.db, audit log, anchor.key, settings.yaml) must be owner-only (0600).

    Their directories, and ``~/.privyx`` with the config it may hold, must be
    writable by their owner alone: anyone else could replace what is in them.
    Only Privyx's own folder is worth a ``chmod 700``: a shared one such as
    ``/tmp`` must stay open, so the files move out of it instead.
    """
    import os
    from pathlib import Path

    if os.name != "posix":
        return (True, "skipped: non-posix platform")

    from privyx.security.keys import default_anchor_secret_path
    from privyx.vaults import SQLiteVault, Vault
    from privyx.vaults._sqlite import sqlite_path

    home = Path(os.path.expanduser("~"), ".privyx").resolve()  # as the folders below
    candidates: list[Path] = [
        default_anchor_secret_path(),
        home / "settings.yaml",
        Path("anchor.key"),
        Path("privyx-audit.log"),
        Path("privyx.db"),
        Path("privyx.db-wal"),
        Path("privyx.db-shm"),
    ]
    vaults = app.registry(Vault)
    if settings.vault.type in vaults and vaults.get(settings.vault.type) is SQLiteVault:
        db_path = sqlite_path(settings.vault.model_dump()["dsn"])
        if db_path != ":memory:":
            candidates.extend([Path(db_path), Path(f"{db_path}-wal"), Path(f"{db_path}-shm")])
    if app.audit.enabled:
        candidates.append(app.audit.path())

    seen: set[Path] = set()
    folders: dict[Path, list[str]] = {home: []}  # each folder, and the files checked in it
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
        folders.setdefault(resolved.parent, []).append(resolved.name)
        if mode & 0o077 != 0:
            insecure.append(f"{path} ({oct(mode & 0o777)}): run 'chmod 600 {path}'")

    for folder, names in folders.items():
        try:
            mode = folder.stat().st_mode
        except OSError:
            continue
        if mode & 0o022 == 0:
            continue
        if folder == home or home in folder.parents:
            insecure.append(f"{folder} ({oct(mode & 0o777)}): run 'chmod 700 {folder}'")
        else:
            insecure.append(
                f"{folder} ({oct(mode & 0o777)}) lets others write there: "
                f"move {', '.join(names)} to a folder only you can write to"
            )

    if insecure:
        return (False, f"insecure file permissions: {'; '.join(insecure)}")
    if checked:
        return (True, f"{checked} sensitive file(s) owner-only (0600)")
    return (True, "sensitive files owner-only (0600)")


def _check_vault(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """The configured vault round-trips a session.

    This is where a bad DSN, a missing driver or an unreachable Redis shows up
    — all of which otherwise surface only at the first request.
    """
    from privyx.app._build import open_vault
    from privyx.core import Session

    async def _probe() -> tuple[bool, str]:
        vault, close = await open_vault(settings, app)
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


def _check_provider(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """The configured provider is registered and constructible."""
    if importlib.util.find_spec("httpx") is None:
        return (False, "httpx not installed")

    from privyx.app._build import build_provider
    from privyx.providers import Provider, resolve_base_url
    from privyx.security import redact

    ptype = settings.provider.type
    registry = app.registry(Provider)
    if ptype not in registry:
        plugins = [f"{n} (plugin)" for n in registry.names() if not registry.is_builtin(n)]
        available = sorted(registry.builtin_names()) + plugins
        return (False, f"unknown provider {ptype!r} (known: {', '.join(available)})")
    build_provider(settings, app)
    return (True, f"{ptype} → {redact(resolve_base_url(settings), 'url')}")


def _check_proxy(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """A request payload survives the proxy with no PII left in it."""
    from privyx.agents import Agent, client_metadata
    from privyx.dialects import Dialect
    from privyx.proxy import transform_request

    async def _probe() -> tuple[bool, str]:
        async with _probing(settings, app) as (engine, session):
            text, detection = await _probe_sample(settings, app, engine.detector, engine.policy)
            if not detection.spans:
                return (True, "skipped: no diagnostic sample is masked by the detector and policy")
            payload = await transform_request(
                {"messages": [{"role": "user", "content": text}]},
                engine,
                session.session_id,
                dialect=app.registry(Dialect).get("openai")(),
                metadata_keys=client_metadata(app.registry(Agent))[0],
            )
            stored = await engine.vault.get(session.session_id)
            transformed = payload["messages"][0]["content"]
            remainder = transformed
            if stored:
                # A token's entity name can itself be a configured term.
                for replacement in sorted(stored.mapping, key=len, reverse=True):
                    remainder = remainder.replace(replacement, "")
            if engine.operator.fixed_text:
                remainder = remainder.replace(engine.operator.fixed_text, "")
            if transformed == text or any(span.text in remainder for span in detection.spans):
                return (False, "diagnostic sample survived the request transform")
            count = len(stored.mapping) if stored else 0
            return (True, f"request transformed, {count} mapping(s) vaulted")

    return asyncio.run(_probe())


def _check_streaming(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """A pseudonym split across SSE chunks is restored before the client sees it.

    The split lands inside the pseudonym on purpose: that is exactly the case a
    stateless SSE parser gets wrong.
    """
    import json

    from privyx.dialects import Dialect
    from privyx.streaming import SSEStreamAdapter, resolver_for, select_processor_factory
    from privyx.streaming.sse import parse_sse

    async def _probe() -> tuple[bool, str]:
        async with _probing(settings, app) as (engine, session):
            text, detection = await _probe_sample(settings, app, engine.detector, engine.policy)
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
            adapter = SSEStreamAdapter(data_path=["choices", "0", "delta", "content"])
            stored = await engine.vault.get(session.session_id)
            mapping = stored.mapping if stored is not None else {}
            resolve = resolver_for(engine.operator, mapping)
            router = (
                app.registry(Dialect)
                .get("openai")()
                .stream(
                    engine.codec,
                    resolve,
                    select_processor_factory(engine.operator, engine.codec, resolve, mapping),
                )
            )
            out = "".join(
                [*router.feed(body[:midpoint]), *router.feed(body[midpoint:]), *router.flush()]
            )
            restored = "".join(adapter.extract_delta(event) for event in parse_sse(out))
            if pseudonym in restored:
                return (False, "pseudonym leaked through the stream")
            if restored != f"to {original} ok":
                return (False, "stream did not restore the original value")
            return (True, "pseudonym restored across a split chunk")

    return asyncio.run(_probe())


def _check_routes(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """Describe effective masking coverage and expose unmasked forwarding."""
    from privyx.app._serve import masked_routes
    from privyx.config.schema import ProxyConfig
    from privyx.dialects import Dialect

    proxy = settings.proxy
    registry = app.registry(Dialect)
    invalid = [
        path
        for path, schema in proxy.routes.items()
        if not path.startswith("/")
        or path.startswith("//")
        or any(c.isspace() or c in "?#\x00" for c in path)
        or schema not in registry
    ]
    if invalid:
        return False, "invalid path or unsupported schema in proxy.routes: " + ", ".join(invalid)
    if not proxy.routes:
        return False, "no masking routes configured"

    routes = masked_routes(app, settings)
    if proxy.mode == "gateway":
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


def _check_anchor_key(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """The automatic anchor key can be read, or created on first use.

    Checked without creating it: a key file that exists must be readable and
    not empty, and a missing one needs a writable directory to be made in.
    """
    from privyx.anchors import Anchor, HMACAnchor
    from privyx.operators import Operator
    from privyx.security.keys import default_anchor_secret_path

    if not app.registry(Operator).get(settings.operator.type).uses_anchor:
        return (True, f"not used by operator={settings.operator.type}")
    if not issubclass(app.registry(Anchor).get(settings.anchor.type), HMACAnchor):
        return (True, f"not used by anchor={settings.anchor.type}")
    path = default_anchor_secret_path()
    try:
        if path.read_text(encoding="utf-8").strip():
            return (True, f"{path}")
        return (False, f"{path} is empty; remove it to create a new key")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        return (False, f"cannot read {path}: {type(exc).__name__}")
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if os.access(parent, os.W_OK | os.X_OK):
        return (True, f"{path} is created on first use")
    return (False, f"cannot create {path}: {parent} is not writable")


def _check_configuration(settings: Settings, app: Privyx) -> tuple[bool, str]:
    """Settings load and validate, and every named component can be built.

    ``build_anchor`` is called here rather than getting a check of its own: an
    unknown ``anchor.type`` is a config error, and this is the check that
    reports config errors.  It also makes the anchor state visible, since
    "anchored or not" changes what pseudonyms look like.
    """
    from privyx.app._build import build_anchor

    anchor = build_anchor(settings, app)  # raises ConfigError on an unknown type
    anchoring = f"anchor={settings.anchor.type}" if anchor else "anchor=off"
    return (
        True,
        f"valid: detector={settings.detector_type} policy={settings.policy.type} "
        f"operator={settings.operator.type} vault={settings.vault.type} {anchoring}",
    )


#: Detector options a missed spelling is retried with.  One is suggested only
#: when the retry masks the whole value, so a suggestion is never a guess.
_OPTIONS = ("underscore_separates_words", "decode", "normalize", "term_variants", "merge_terms")

#: Fake values of the built-in entities that appear inside identifiers
#: (``host_10.0.0.1``).  The others are found by what surrounds them.
_SAMPLES = {
    "IP_ADDRESS": "10.0.0.1",
    "SSN": "123-45-6789",
    "CREDIT_CARD": "4111111111111111",
    "API_KEY": "sk-" + "proj-abcdefghijklmnopqrstuvwx",
}

#: A term written as words split by spaces, dots, dashes, or underscores.
_WORDS = re.compile(r"[^\W_]+(?:[ ._-]+[^\W_]+)*")
_FULLWIDTH = {c: c + 0xFEE0 for c in range(0x21, 0x7F)}

#: A spelling as (text, sensitive) parts: the sensitive parts must be masked.
Parts = tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class Missed:
    """A spelling not fully masked: ``missed`` (nothing of it) or ``partly``."""

    state: str
    entity: str
    text: str


@dataclass(frozen=True)
class Lint:
    """What ``privyx config lint`` found."""

    #: The config's own warnings, and terms so short they mask common words.
    warnings: list[str]
    #: The detectors that are on but not checked: not of type ``regex``.
    unchecked: list[str]
    #: The detectors checked: those of type ``regex``.
    checked: list[str] = field(default_factory=list)
    #: Each advice, with the spellings it masks, in the order first seen.
    advice: dict[str, list[Missed]] = field(default_factory=dict)
    spellings: int = 0
    terms: int = 0
    samples: int = 0

    @property
    def leaks(self) -> int:
        return sum(len(missed) for missed in self.advice.values())


@dataclass(frozen=True)
class _Probe:
    entity: str
    parts: Parts

    @property
    def text(self) -> str:
        return "".join(text for text, _ in self.parts)

    def sensitive(self) -> set[int]:
        positions: set[int] = set()
        at = 0
        for text, secret in self.parts:
            if secret:
                positions.update(range(at, at + len(text)))
            at += len(text)
        return positions


async def _lint(app: Privyx) -> Lint:
    from privyx.app._build import build_detector, build_policy
    from privyx.app._plugins import load_plugins
    from privyx.config.schema import DetectorConfig

    loaded = app._read(app._config)
    settings, warnings = loaded.settings, loaded.warnings
    load_plugins(settings, app)
    sections = settings.active_detectors
    checked = {n: s.model_dump() for n, s in sections.items() if is_regex(app, s.type)}
    unchecked = [f"{n} ({s.type})" for n, s in sections.items() if not is_regex(app, s.type)]
    if not checked:
        return Lint(warnings, unchecked)

    policy = build_policy(settings, app)
    off = frozenset(o for o in _OPTIONS if not all(c.get(o) for c in checked.values()))
    built: dict[frozenset[str], Detector] = {}
    where = (
        [f"detectors.{name}." for name in checked]
        if len(checked) == 1
        else ["detectors.<each regex>."]
    )[0]

    def detector_with(options: frozenset[str]) -> Detector:
        if options not in built:
            on = dict.fromkeys(options, True)
            built[options] = build_detector(
                {n: DetectorConfig.model_validate({**c, **on}) for n, c in checked.items()}, app
            )
        return built[options]

    async def masks(probe: _Probe, options: frozenset[str], policy: Policy | None) -> bool:
        return not await _missing(probe, detector_with(options), policy)

    async def advice_for(probe: _Probe) -> str:
        """The options that mask ``probe``: each one alone, else the fewest together."""
        fixes = [o for o in sorted(off) if await masks(probe, frozenset({o}), policy)]
        joiner = " or "
        if not fixes and await masks(probe, off, policy):
            fix = set(off)
            for o in sorted(off):
                if await masks(probe, frozenset(fix - {o}), policy):
                    fix.discard(o)
            fixes, joiner = sorted(fix), " and "
        if fixes:
            return "Set " + joiner.join(f"{where}{o}: true" for o in fixes)
        if await masks(probe, off, None):
            return f"Found, but {policy.reason()}"
        return "No option masks these; add them to the terms"

    listed = {
        (e, str(v).strip()) for c in checked.values() for e, vs in c["terms"].items() for v in vs
    }
    terms = sorted((entity, term) for entity, term in listed if term)
    probes: list[_Probe] = []
    for entity, term in terms:
        if len(term) <= 2:
            warnings.append(f"{entity} term {term!r} masks every standalone {term!r}")
        probes += [_Probe(entity, parts) for parts in _spellings(term)]
    samples = 0
    for entity, value in _SAMPLES.items():
        # A built-in the config turns off, or the policy drops, is not checked.
        if await masks(_Probe(entity, ((value, True),)), frozenset(), policy):
            samples += 1
            for parts in ((("id_", False), (value, True)), ((value, True), ("_old", False))):
                probes.append(_Probe(entity, parts))

    advice: dict[str, list[Missed]] = {}
    for probe in probes:
        missing = await _missing(probe, detector_with(frozenset()), policy)
        if not missing:
            continue
        state = "partly" if missing != probe.sensitive() else "missed"
        advice.setdefault(await advice_for(probe), []).append(
            Missed(state, probe.entity, probe.text)
        )
    return Lint(warnings, unchecked, list(checked), advice, len(probes), len(terms), samples)


def _spellings(term: str) -> list[Parts]:
    """``term`` as written and in the spellings that tend to slip past a term list."""
    if not _WORDS.fullmatch(term):
        return [((term, True),)]
    pieces = re.split(r"([ ._-]+)", term)
    written: Parts = tuple((piece, i % 2 == 0) for i, piece in enumerate(pieces))
    upper: Parts = tuple((text.upper(), secret) for text, secret in written)
    words = pieces[::2]
    first = words[0]
    spellings = [
        written,
        (("DB_", False), *upper),
        (*written, ("_dev", False)),
        tuple((text.translate(_FULLWIDTH), secret) for text, secret in written),
    ]
    if len(first) > 1:
        spellings.append(((first[0], True), ("​", False), (first[1:], True), *written[1:]))
    if len(words) > 1:
        for separator in ("_", "__", "%20"):
            joined = [((separator, False), (word, True)) for word in words[1:]]
            spellings.append(((first, True), *(part for pair in joined for part in pair)))
    return list(dict.fromkeys(spellings))


async def _missing(probe: _Probe, detector: Detector, policy: Policy | None) -> set[int]:
    """The sensitive positions of ``probe`` the detector and policy leave unmasked."""
    from privyx.core import Context

    text, context = probe.text, Context()
    detection = await detector.detect(text, context)
    if policy is not None:
        detection = await policy.decide(detection, context)
    masked = {i for s in detection.merged(text).spans for i in range(s.start, s.end)}
    return probe.sensitive() - masked
