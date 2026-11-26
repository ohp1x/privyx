"""The audit service: the totals and records of the audit trail, and the tap's records.

``privyx audit`` and the ``status`` and ``audit-tail`` prompts of
``privyx mcp serve`` call it.  The trail holds counts and entity types only.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.features import AuditStats


@dataclass(frozen=True)
class Totals:
    """The totals of an audit trail, and how many of its lines were not records."""

    path: Path
    stats: AuditStats
    skipped: int


class Audit:
    def __init__(self, app: Privyx) -> None:
        self._app = app

    @property
    def enabled(self) -> bool:
        """Whether the audit trail is written (``features.audit``)."""
        return "audit" in self._app.features.configured()

    @property
    def tapping(self) -> bool:
        """Whether the tap records each masked exchange (``proxy.tap``)."""
        return bool(self._app.settings.proxy.tap)

    def path(self, file: Path | None = None) -> Path:
        """``file``, or the configured ``features.audit.path``."""
        return file or Path(os.path.expanduser(self._app.features.options("audit").path))

    def totals(self, file: Path | None = None, *, since: float | None = None) -> Totals:
        """The totals of the trail, of the last ``since`` seconds when given.

        Raises:
            OSError: If the trail cannot be read.
        """
        from privyx.features import AuditStats

        cutoff = time.time() - since if since else None
        stats, skipped = AuditStats(), 0
        path = self.path(file)
        for record in self.records(path):
            try:
                if record is None:
                    raise ValueError
                if cutoff is None or record["ts"] >= cutoff:
                    stats.add(record)
            except (KeyError, TypeError, ValueError):
                skipped += 1
        return Totals(path, stats, skipped)

    def records(self, file: Path | None = None) -> Iterator[dict[str, Any] | None]:
        """Each line of the trail as a record, or ``None`` for one that is not.

        Raises:
            OSError: If the trail cannot be read.
        """
        with self.path(file).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    record = None
                yield record if isinstance(record, dict) else None

    def conversation(self, native: str, file: Path | None = None) -> list[dict[str, Any]]:
        """The records of the requests of one agent conversation, in the trail's order.

        ``native`` is the agent's own ID of it, such as Claude Code's session
        ID: its ``conversation_ref`` is computed again, for each agent type
        that sends one, and matched on ``proxy.request``.

        Raises:
            ConfigError: If the trail's key cannot be read.
            OSError: If the trail cannot be read.
        """
        from privyx.agents import CONVERSATION_AGENTS
        from privyx.features._audit import conversation_ref, trail_key

        path = self.path(file)
        key = trail_key(self._app.features.options("audit"), path)
        refs = {conversation_ref(key, agent, native) for agent in CONVERSATION_AGENTS}
        requests = {
            record.get("request_id")
            for record in self.records(path)
            if record is not None
            and record.get("event") == "proxy.request"
            and record.get("conversation_ref") in refs
        }
        requests.discard(None)
        return [
            record
            for record in self.records(path)
            if record is not None and record.get("request_id") in requests
        ]

    def tap_files(self, session_id: str | None = None) -> list[Path]:
        """The tap files of ``session_id``, or of every session."""
        from privyx.proxy._tap import tap_file

        directory = Path(os.path.expanduser(self._app.settings.proxy.tap_dir))
        if session_id is not None:
            return [tap_file(directory, session_id)]
        return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
