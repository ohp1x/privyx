"""The masking service: masking and restoring text, JSON, and JSON lines, and detection.

``privyx mask`` and ``unmask`` and ``privyx mcp serve`` call it, so what they
do to a file is exactly what the proxy does to a chat message.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core import Context, Detection, PrivyxError, Session, SessionNotFoundError

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.engine import PrivacyEngine

_EXTENSIONS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}

#: Applied to one string leaf; async because the engine is.
Transform = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class Masked:
    """Masked text, and the session holding the mapping that restores it."""

    text: str
    session: Session


@dataclass(frozen=True)
class MaskedDocument:
    """A masked document, and the vault session that restores it, if any.

    With the memory vault, no session outlives the call: ``session_id`` is
    ``None``, and the output can never be restored.
    """

    text: str
    session_id: str | None


@dataclass(frozen=True)
class Finding:
    """A sensitive span, by position and type only: never its value."""

    start: int
    end: int
    category: str


@dataclass(frozen=True)
class Findings:
    findings: list[Finding]
    #: The spans of other types than the ones asked for, when some were.
    filtered_out: int | None = None


class Masking:
    """Masking with the app's engine; each call opens it, unless it is open already."""

    def __init__(self, app: Privyx) -> None:
        self._app = app

    @staticmethod
    def format_of(fmt: str, path: str | None) -> str:
        """``fmt``, or with ``auto`` the format the extension of the input ``path`` says."""
        if fmt != "auto":
            return fmt
        if path and path != "-":
            return _EXTENSIONS.get(Path(path).suffix.lower(), "text")
        return "text"

    async def detect(self, text: str, *, categories: list[str] | None = None) -> Findings:
        """The spans of ``text`` the configured detector and policy mask.

        With ``categories``, only the spans of those types (case-insensitive);
        how many others there were is counted, so a name that matches no type
        (``PASSWORD`` for SECRET) does not read as clean text.
        """
        async with self._app.engine() as engine:
            context = Context()
            detection = await engine.policy.decide(
                await engine.detector.detect(text, context), context
            )
        filtered_out = None
        if categories is not None:
            wanted = {c.upper() for c in categories}
            left = Detection([s for s in detection.spans if s.entity_type.upper() not in wanted])
            filtered_out = len(left.merged(text).spans)
            detection = Detection([s for s in detection.spans if s.entity_type.upper() in wanted])
        return Findings(
            [Finding(s.start, s.end, s.entity_type) for s in detection.merged(text).spans],
            filtered_out,
        )

    async def mask(
        self,
        source: str,
        *,
        fmt: str = "text",
        paths: tuple[str, ...] = (),
        session: Session | None = None,
        session_id: str | None = None,
    ) -> Masked:
        """``source`` masked in ``session``, a mapping loaded from a file, or in a vault session.

        The vault session is ``session_id``, or a new one without it.
        """
        async with self._app.engine() as engine:
            held = (
                session if session is not None else await engine.get_or_create_session(session_id)
            )
            return Masked(await apply(source, fmt, paths, *_maskers(engine, held)), held)

    async def unmask(
        self,
        source: str,
        *,
        fmt: str = "text",
        paths: tuple[str, ...] = (),
        session: Session | None = None,
        session_id: str | None = None,
    ) -> str:
        """``source`` with the originals back, from ``session`` or vault session ``session_id``.

        Tokens the mapping does not know are left untouched.

        Raises:
            SessionNotFoundError: If the vault has no session ``session_id``.
        """
        async with self._app.engine() as engine:
            # Resolved once, not once per leaf: a JSON document can hold
            # thousands of strings and every one of them would hit the vault.
            held = session if session is not None else await engine.vault.get(session_id or "")
            if held is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            context = Context(session_id=held.session_id, vault=engine.vault)

            async def restore(leaf: str) -> str:
                return (await engine.operator.deanonymize(leaf, held, context)).text

            return await apply(source, fmt, paths, restore)

    async def mask_document(
        self, source: str, *, fmt: str, write: Callable[[str], Awaitable[None]] | None = None
    ) -> MaskedDocument:
        """``source`` masked in a session of its own, then passed to ``write`` when given.

        With a persistent vault the session is kept, so the output can be
        restored; with the memory vault nothing outside this call could read
        the mapping, so it stays out of the vault and goes with the call.  When
        masking or ``write`` fails, the session is deleted.
        """
        persistent = self._app.sessions.persistent
        async with self._app.engine() as engine:
            if persistent:
                sid = (await engine.get_or_create_session()).session_id
                # One vault read and write for the document, not one per JSON leaf.
                scope: Any = engine.session_scope(sid, require_existing=True)
            else:
                ephemeral = engine.ephemeral_session()
                scope = contextlib.nullcontext(ephemeral)
            try:
                async with scope as session:
                    result = await apply(
                        source, fmt, (), *_maskers(engine, session, persist=False)
                    )
                if write is not None:
                    await write(result)
            except BaseException:
                if persistent:
                    # The caller hears why the call failed, not why cleanup did.
                    with contextlib.suppress(PrivyxError):
                        await engine.delete_session(sid, reason="sanitize_failed")
                raise
            finally:
                if not persistent:
                    engine.end_ephemeral_session(ephemeral)
        return MaskedDocument(result, sid if persistent else None)


def _maskers(
    engine: PrivacyEngine, session: Session, **options: Any
) -> tuple[Transform, Callable[[str, str], Awaitable[str]]]:
    """Masking of a leaf in ``session``, and of a leaf under a key.

    ``{"password": "hunter2"}``: the value is a secret by its key.
    """

    async def transform(leaf: str) -> str:
        return (await engine.transform(leaf, session=session, **options)).text

    async def under(key: str, leaf: str) -> str:
        return (await engine.transform(leaf, session=session, key=key, **options)).text

    return transform, under


async def apply(
    source: str,
    fmt: str,
    paths: tuple[str, ...],
    fn: Transform,
    under: Callable[[str, str], Awaitable[str]] | None = None,
) -> str:
    """Apply ``fn`` to every string the chosen format exposes.

    ``under(key, text)``, when given, stands in for ``fn`` on a JSON string or
    number under a key (see :func:`~privyx.core.map_values`).
    """
    if fmt == "text":
        return await fn(source)
    if fmt == "jsonl":
        lines = []
        for line in source.splitlines():
            if not line.strip():
                lines.append(line)
                continue
            lines.append(dumps(await _apply_json(json.loads(line), paths, fn, under)))
        return "\n".join(lines)
    return dumps(await _apply_json(json.loads(source), paths, fn, under), indent=2)


async def _apply_json(
    payload: Any,
    paths: tuple[str, ...],
    fn: Transform,
    under: Callable[[str, str], Awaitable[str]] | None,
) -> Any:
    """Apply ``fn`` to every string leaf of a JSON document."""
    from privyx.core import map_values

    return await map_values(payload, fn, under, paths=list(paths) or None)


def dumps(payload: Any, indent: int | None = None) -> str:
    """``payload`` as JSON, non-ASCII text kept as it is."""
    return json.dumps(payload, ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class Spotted:
    """A span ``privyx detect`` shows: where, its type, and its text."""

    start: int
    end: int
    entity: str
    text: str


@dataclass(frozen=True)
class Explained:
    """A span, the detectors and rules that found it, and what the policy does with it."""

    span: Spotted
    found_by: list[str]
    #: ``masked``, ``partly masked: ...``, or ``not masked: ...``.
    outcome: str


@dataclass(frozen=True)
class PatternCheck:
    """A regex: whether it can backtrack catastrophically, and how fast it scans."""

    entity: str | None
    pattern: str
    risky: bool
    #: Microseconds per KB of ordinary text, or ``None``: still scanning after ``timeout``.
    speed: float | None
    #: Seconds the scan may take.
    timeout: float


#: Text the regex benchmark scans: prose, code, JSON, and a log line, with no
#: personal data, so the timing reflects the text a pattern mostly sees.
_SAMPLE = (
    "The quarterly report is due on Friday; please review section 3 before the call.\n"
    "def handler(event, context):\n    return {'status': 200, 'body': event['body']}\n"
    '{"id": 4821, "items": [{"sku": "A-100", "qty": 2}], "note": "ship by noon"}\n'
    "2026-01-15T09:30:00Z INFO worker-7 processed 128 jobs in 3.2s (retries=0)\n"
)

#: Seconds the regex benchmark may run before the pattern is reported as stalled.
BENCH_TIMEOUT = 5.0

_BENCH = """
import re, sys, time
pattern = re.compile(sys.argv[1])
text = sys.stdin.read()
best = float("inf")
for _ in range(5):
    start = time.perf_counter()
    for _ in pattern.finditer(text):
        pass
    best = min(best, time.perf_counter() - start)
print(best)
"""


class Inspection:
    """Detection shown span by span, for ``privyx detect``: no vault is opened."""

    def __init__(self, app: Privyx) -> None:
        self._app = app

    async def spans(self, text: str, *, policy: bool = True) -> list[Spotted]:
        """The spans of ``text`` the configured detectors find, and the policy keeps."""
        detection, _ = await self._detected(text, policy)
        return [_spotted(s) for s in detection.merged(text).spans]

    async def _detected(self, text: str, policy: bool) -> tuple[Detection, Context]:
        """What the configured detectors find in ``text`` and the policy keeps, and its context."""
        from privyx.app._build import build_detector, build_policy
        from privyx.app._plugins import load_plugins

        settings = self._app.settings
        load_plugins(settings, self._app)
        context = Context()
        detection = await build_detector(settings.detectors, self._app).detect(text, context)
        if policy:
            detection = await build_policy(settings, self._app).decide(detection, context)
        return detection, context

    async def preview(self, text: str, *, policy: bool = True) -> str:
        """``text`` masked in a session of its own, which no vault keeps."""
        from privyx.app._build import open_operator

        detection, context = await self._detected(text, policy)
        operator = await open_operator(self._app.settings, self._app)
        try:
            return (await operator.pseudonymize(text, detection, Session(), context)).text
        finally:
            await operator.close()

    async def explain(self, text: str, *, policy: bool = True) -> list[Explained]:
        """Each span, with the detectors that found it and the policy's decision.

        Each configured detector runs on its own, so a span can be traced to
        the one that found it; together they find what the configured
        detectors do.
        """
        from privyx.app._build import build_detector, build_policy
        from privyx.app._plugins import load_plugins
        from privyx.app._services._diagnostics import is_regex
        from privyx.core import BUILTIN_ENTITIES

        settings = self._app.settings
        load_plugins(settings, self._app)
        context = Context()

        async def detect(name: str, config: dict[str, Any]) -> Detection:
            detector = build_detector({name: config}, self._app)
            return await detector.detect(text, context)

        sections = settings.active_detectors
        found: list[tuple[Any, str]] = []
        for name, section in sections.items():
            label = name if name == section.type else f"{name} ({section.type})"
            config = section.model_dump()
            options = _PatternOptions(config)
            regex = is_regex(self._app, section.type)
            detection = await detect(name, config)
            plain = decoded = detection
            if options.decode or options.normalize:
                plain = await detect(name, {**config, "decode": False, "normalize": False})
            if options.decode and options.normalize:
                decoded = await detect(name, {**config, "normalize": False})
            direct = {(s.start, s.end, s.entity_type) for s in plain.spans}
            hidden = (
                {(s.start, s.end, s.entity_type) for s in decoded.spans}
                if options.decode
                else set()
            )
            # Merged with terms, a pattern's entity is credited to the terms they find alone.
            both = options.terms.keys() & options.patterns.keys() if options.merge_terms else set()
            by_terms: set[tuple[int, int, str]] = set()
            if both and regex:
                # The terms alone: no pattern of theirs, nor the built-in it replaced.
                patterns = {k: v for k, v in options.patterns.items() if k not in both}
                builtin = both & BUILTIN_ENTITIES
                alone = await detect(
                    name,
                    {
                        **config,
                        "patterns": patterns,
                        "enable": [e for e in options.enable if e not in both],
                        "disable": sorted({*options.disable, *builtin}),
                    },
                )
                by_terms = {(s.start, s.end, s.entity_type) for s in alone.spans}
            for s in detection.spans:
                where = (s.start, s.end, s.entity_type)
                merged = where in by_terms and s.entity_type in both
                reason = f"{label}: {_origin(regex, options, s.entity_type, merged)}"
                if where not in direct:
                    reason += ", once joined or decoded" if where in hidden else ", once folded"
                found.append((s, reason))

        raw = Detection([span for span, _ in found])
        chosen = build_policy(settings, self._app) if policy else None
        kept = await chosen.decide(raw, context) if chosen is not None else raw
        masked = kept.merged(text).spans
        explained = []
        for span in raw.merged(text).spans:
            reasons = [reason for part, reason in found if _overlap(part, span)]
            # The masked spans lie within the shown ones: the policy only drops spans.
            parts = [m for m in masked if _overlap(m, span)]
            if not parts:
                assert chosen is not None  # only a policy drops a span
                outcome = f"not masked: {chosen.reason()}"
            elif sum(m.length for m in parts) == span.length:
                outcome = "masked"
            else:
                only = ", ".join(f"{m.text!r} as {m.entity_type}" for m in parts)
                outcome = f"partly masked: only {only}; the rest is sent as is"
            explained.append(Explained(_spotted(span), reasons, outcome))
        return explained

    def check_pattern(
        self, pattern: str | None = None, *, entity: str | None = None
    ) -> PatternCheck:
        """Check ``pattern``, or the configured regex of ``entity``.

        The speed is measured in a child process: a regex cannot be stopped
        from inside this one, and the static check does not catch every
        pattern that backtracks.

        Raises:
            ConfigError: If the config has no pattern for ``entity``, or the
                regex is invalid.
        """
        import re

        from privyx.core import ConfigError
        from privyx.detectors._pattern import backtracks

        if entity is not None:
            configured = self.patterns()
            if entity not in configured:
                raise ConfigError(f"no regex detector of the config detects {entity!r}")
            pattern = configured[entity]
        assert pattern is not None
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex: {exc}") from exc
        text = _SAMPLE * (64 * 1024 // len(_SAMPLE))
        best = _bench(pattern, text)
        speed = None if best is None else best / (len(text) / 1024) * 1e6
        return PatternCheck(entity, pattern, backtracks(pattern), speed, BENCH_TIMEOUT)

    def patterns(self) -> dict[str, str]:
        """Entity → regex of every regex detector that is on, built-ins included."""
        from privyx.app._services._diagnostics import is_regex
        from privyx.detectors._pattern import _builtins, _enabled, _patterns_from

        patterns: dict[str, str] = {}
        for section in self._app.settings.active_detectors.values():
            if not is_regex(self._app, section.type):
                continue
            config = section.model_dump()
            own = {**_enabled(config), **_patterns_from(config)}
            builtins = _builtins(config) if config.get("builtins", True) else {}
            patterns.update({**builtins, **own})
        return patterns


def _spotted(span: Any) -> Spotted:
    return Spotted(span.start, span.end, span.entity_type, span.text)


class _PatternOptions:
    """The pattern options of a detector section, as their defaults for a type without them."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.decode = bool(config.get("decode"))
        self.normalize = bool(config.get("normalize"))
        self.terms: dict[str, Any] = config.get("terms") or {}
        self.patterns: dict[str, Any] = config.get("patterns") or {}
        self.merge_terms = bool(config.get("merge_terms", True))
        self.enable: list[str] = config.get("enable") or []
        self.disable: list[str] = config.get("disable") or []
        self.builtins = bool(config.get("builtins", True))


def _origin(regex: bool, options: _PatternOptions, entity: str, by_terms: bool) -> str:
    """Which rule of a detector found a span: a pattern, a term list, or a built-in.

    ``regex``: it is the built-in ``regex`` detector, the only one with rules.
    ``by_terms``: the terms find the span alone, which decides for an entity
    whose terms and pattern are merged.
    """
    from privyx.detectors import DEFAULT_PATTERNS

    if not regex:
        return entity
    if entity in options.patterns and not by_terms:
        return f"pattern {entity}"
    if entity in options.terms:
        return f"terms {entity}"
    if (options.builtins and entity in DEFAULT_PATTERNS) or entity in options.enable:
        return f"built-in pattern {entity}"
    return entity


def _overlap(a: Any, b: Any) -> bool:
    return bool(a.start < b.end and b.start < a.end)


def _bench(pattern: str, text: str) -> float | None:
    """Seconds for the best of five scans of ``text``; ``None`` past :data:`BENCH_TIMEOUT`."""
    import subprocess
    import sys

    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _BENCH, pattern],
            input=text,
            capture_output=True,
            text=True,
            timeout=BENCH_TIMEOUT,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return None
    return float(done.stdout)
