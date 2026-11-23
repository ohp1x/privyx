"""The sessions service: the vault's sessions, and what the audit trail and the tap hold of one.

``privyx session`` and the ``status`` prompt of ``privyx mcp serve`` call it.
Nothing here returns an original value unless a caller asks for a session's
mapping itself.
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core import PrivyxError, Session, SessionBusyError

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.token import LogicalToken
    from privyx.vaults import Vault

#: Tap record fields kept in a forensic transcript: never ``before``/``restored``.
_MASKED_SIDE = (
    "time",
    "ts",
    "request_id",
    "session_id",
    "stage",
    "path",
    "schema",
    "status",
    "stream",
    "aborted",
    "after",
    "upstream",
)


@dataclass(frozen=True)
class Usage:
    """A session's responses in the audit trail, their tokens and estimated cost by model.

    A model without a price in ``features.audit.prices`` costs ``None``.
    """

    responses: int
    by_model: dict[str, dict[str, int]]
    costs: dict[str, float | None]


@dataclass(frozen=True)
class SessionView:
    """One session: its vault record, if any, and its usage from the audit trail, if any."""

    session_id: str
    record: Session | None
    usage: Usage | None


@dataclass(frozen=True)
class Exported:
    """A forensic archive written: how many audit events and transcript records it holds."""

    path: Path
    events: int
    transcript: int


@dataclass(frozen=True)
class TokenFound:
    """What a token stands for: its parts, if it is one, and the sessions that hold it."""

    #: The token read in the configured format, or ``None`` if it is not one.
    parsed: LogicalToken | None
    #: The configured format.
    format: str
    sessions: list[Session]
    #: Seconds an idle session lives, or ``None``: it never expires.
    ttl: int | None


@dataclass(frozen=True)
class Pruned:
    """The sessions idle since before ``cutoff``: those deleted, or a dry run's candidates."""

    sessions: list[Session]
    cutoff: float


class Sessions:
    def __init__(self, app: Privyx) -> None:
        self._app = app

    @property
    def vault_type(self) -> str:
        return str(self._app.settings.vault.type)

    @property
    def persistent(self) -> bool:
        """Whether the vault's sessions outlive this process, as its options say.

        A vault that cannot be built counts as persistent: opening it reports why.
        """
        from privyx.app._build import build_vault
        from privyx.core import ConfigError

        try:
            return build_vault(self._app.settings, self._app).persistent
        except ConfigError:
            return True

    async def list(self) -> list[Session]:
        """The live sessions, most recently active first.

        Raises:
            PrivyxError: If the vault cannot be opened or listed.
        """
        async with self._vault() as vault:
            sessions = await vault.list_sessions()
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    async def find_token(self, token: str) -> TokenFound:
        """The sessions whose mapping holds ``token``, and the token read in the configured format.

        Raises:
            PrivyxError: If the vault cannot be opened or listed.
        """
        from privyx.app._build import build_codec

        settings = self._app.settings
        sessions = [s for s in await self.list() if token in s.mapping]
        parsed = build_codec(settings, self._app).parse(token)
        ttl = settings.vault.model_dump().get("ttl")
        return TokenFound(parsed, settings.token.format, sessions, ttl)

    async def get(self, session_id: str) -> SessionView:
        """Session ``session_id``: its vault record and its usage, either of them ``None``."""
        async with self._vault() as vault:
            record = await vault.get(session_id)
        return SessionView(session_id, record, self._usage(session_id))

    async def export(self, session_id: str, output: Path) -> Exported | None:
        """Write the forensic archive of ``session_id`` to ``output``, or ``None`` if empty.

        ``events.jsonl`` holds its audit events, ``transcript.jsonl`` each
        request as sent upstream and each reply as the upstream sent it (from
        the tap), and ``summary.json`` the totals.  Original values are left out.

        Raises:
            PrivyxError: If ``output`` exists already or cannot be written.
        """
        async with self._vault() as vault:
            record = await vault.get(session_id)
        return self._archive(session_id, None if record is None else len(record.mapping), output)

    def _archive(self, session_id: str, mappings: int | None, output: Path) -> Exported | None:
        from privyx.features import AuditStats
        from privyx.proxy._tap import tap_file
        from privyx.version import __version__

        settings = self._app.settings
        events = matching_lines(self._app.audit.path(), session_id)
        taps = matching_lines(
            tap_file(Path(settings.proxy.tap_dir).expanduser(), session_id), session_id
        )
        transcript = [{k: t[k] for k in _MASKED_SIDE if k in t} for t in taps]
        if mappings is None and not events and not transcript:
            return None

        totals = AuditStats()
        for event in events:
            totals.add(event)
        stamps = [e["ts"] for e in [*events, *transcript] if isinstance(e.get("ts"), int | float)]
        summary = {
            "session_id": session_id,
            "exported_at": datetime.now(UTC).isoformat(timespec="microseconds"),
            "privyx_version": __version__,
            "first_ts": min(stamps, default=None),
            "last_ts": max(stamps, default=None),
            "events": dict(totals.events),
            "entities_masked": dict(totals.entities),
            "pseudonyms_restored": totals.restored,
            "tokens": dict(totals.tokens),
            "errors": dict(totals.errors),
            "transcript_records": dict(Counter(str(t.get("stage")) for t in transcript)),
            "vault_mappings": mappings,
        }

        def jsonl(rows: list[dict[str, Any]]) -> str:
            return "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)

        try:
            # Owner-only, and never over an existing file: it may be evidence.
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise PrivyxError(f"cannot write {output}: {exc.strerror or exc}") from exc
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("summary.json", json.dumps(summary, indent=2) + "\n")
            archive.writestr("events.jsonl", jsonl(events))
            archive.writestr("transcript.jsonl", jsonl(transcript))
        return Exported(output, len(events), len(transcript))

    async def prune(self, older_than: float, *, dry_run: bool = False) -> Pruned:
        """Delete the sessions idle at least ``older_than`` seconds, or with ``dry_run`` list them.

        Each deletion is written to the audit trail as ``session.deleted``
        with ``reason: prune``, after it succeeded.  A session used since it
        was listed, or held by a request right now, is kept.
        """
        cutoff = time.time() - older_than
        async with self._vault() as vault:
            stale = [s for s in await vault.list_sessions() if s.updated_at < cutoff]
            if dry_run:
                return Pruned(stale, cutoff)
            deleted: list[Session] = []
            # The audit trail alone, for the deletions.
            features = self._app.features.running(self._app.settings, only={"audit"})
            async with features as audit:
                for s in stale:
                    session = await _delete_if_idle(vault, s.session_id, cutoff)
                    if session is None:
                        continue
                    deleted.append(session)
                    # After the delete succeeds, so the trail never claims a
                    # deletion that failed (same contract as the proxy).
                    audit.session_deleted(
                        session.session_id, reason="prune", mapping_count=len(session.mapping)
                    )
        return Pruned(deleted, cutoff)

    @asynccontextmanager
    async def _vault(self) -> AsyncIterator[Vault]:
        """The configured vault, open; the plugins the config names set up first.

        The vault of the app's open engine, when one is open: a server holds
        one connection for all its calls.
        """
        from privyx.app._build import open_vault
        from privyx.app._plugins import load_plugins

        if self._app._engine is not None:
            async with self._app.engine() as engine:
                yield engine.vault
            return
        settings = self._app.settings
        # Register plugin component types so a configured plugin vault resolves.
        load_plugins(settings, self._app)
        vault, close = await open_vault(settings, self._app)
        try:
            yield vault
        finally:
            await close()

    def _usage(self, session_id: str) -> Usage | None:
        """Responses and token usage by model of ``session_id``, from the audit trail.

        ``None`` when the trail is off, missing, or has no response of the session.
        """
        if not self._app.audit.enabled:
            return None
        path = self._app.audit.path()
        responses = [
            e for e in matching_lines(path, session_id) if e.get("event") == "proxy.response"
        ]
        if not responses:
            return None
        by_model: dict[str, Counter[str]] = {}
        for event in responses:
            usage = event.get("usage")
            if isinstance(usage, dict):
                tokens = by_model.setdefault(str(event.get("model") or "unknown"), Counter())
                tokens.update({k: v for k, v in usage.items() if isinstance(v, int)})
        from privyx.observability.usage import cost

        prices = self._app.features.options("audit").prices
        return Usage(
            len(responses),
            {model: dict(tokens) for model, tokens in by_model.items()},
            {model: cost(model, tokens, prices) for model, tokens in by_model.items()},
        )


async def _delete_if_idle(vault: Vault, session_id: str, cutoff: float) -> Session | None:
    """Delete the session if it is still idle since ``cutoff``; the session deleted, if any.

    Read again under the vault's lock, when it has one, since a request may
    have used it after it was listed.
    """

    async def delete() -> Session | None:
        session = await vault.get(session_id)
        if session is None or session.updated_at >= cutoff:
            return None
        await vault.delete(session_id)
        return session

    lock = vault.lock(session_id)
    if lock is None:
        return await delete()
    try:
        async with lock:
            return await delete()
    except SessionBusyError:
        return None  # a request holds it: it is in use


def matching_lines(path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    """The JSON objects in a JSONL file, those of ``session_id`` when given."""
    # A cheap text test first, on the id as the trail and the tap write it
    # (JSON-escaped: ``session"one`` is ``session\\"one`` there), then the parsed field.
    needle = None if session_id is None else json.dumps(session_id, ensure_ascii=False)
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if needle is not None and needle not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and (
                    session_id is None or row.get("session_id") == session_id
                ):
                    rows.append(row)
    except OSError:
        return []
    return rows
