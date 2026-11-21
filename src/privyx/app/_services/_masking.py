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

#: Input formats.  ``auto`` looks at the input file's extension.
FORMATS = ("auto", "text", "json", "jsonl")

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
        return resolve_format(fmt, path)

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

            async def transform(leaf: str) -> str:
                return (await engine.transform(leaf, session=held)).text

            async def under(key: str, leaf: str) -> str:
                # `{"password": "hunter2"}`: the value is a secret by its key.
                return (await engine.transform(leaf, session=held, key=key)).text

            return Masked(await apply(source, fmt, paths, transform, under), held)

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
        persistent = self._app.settings.vault.type != "memory"
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

                    async def transform(leaf: str) -> str:
                        return (await engine.transform(leaf, session=session, persist=False)).text

                    async def under(key: str, leaf: str) -> str:
                        # `{"password": "hunter2"}`: the value is a secret by its key.
                        masked = await engine.transform(
                            leaf, session=session, persist=False, key=key
                        )
                        return masked.text

                    result = await apply(source, fmt, (), transform, under)
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


def resolve_format(fmt: str, input_path: str | None) -> str:
    """``fmt``, or with ``auto`` the format the input file's extension says."""
    if fmt != "auto":
        return fmt
    if input_path and input_path != "-":
        return _EXTENSIONS.get(Path(input_path).suffix.lower(), "text")
    return "text"


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
