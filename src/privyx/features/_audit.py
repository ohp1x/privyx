"""The ``audit`` feature: the audit trail, one JSON line per notification.

Each line is written and flushed as the notification is emitted, never
queued, and not synced to disk.  A line that cannot be written, on a full
disk, is lost and the request goes on: the failure is logged and notified
(:class:`~privyx.events.AuditWriteFailed`, which ``GET /metrics`` counts).  The
file is created owner-only: it names sessions and the upstream host, never a
value.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, TextIO

from pydantic import Field

from privyx.config.schema import ModelPrice
from privyx.core import ConfigError, EventBus, Services, listen
from privyx.events import AuditEvent, AuditWriteFailed
from privyx.features._base import Feature
from privyx.security.keys import make_private_dir

_log = logging.getLogger("privyx")


class AuditTrail(Feature):
    """Appends each audit notification to ``path`` as a JSON line."""

    name = "audit"
    enabled_by_default = True

    class Options(Feature.Options):
        path: str = "~/.privyx/audit.log"
        #: Model-id prefix -> USD per million tokens, for ``privyx session show``.
        prices: dict[str, ModelPrice] = Field(default_factory=dict)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._writer: TextIO | None = None
        self._events: EventBus | None = None
        # One write() per line, so lines of concurrent requests never mix.
        self._lock = threading.Lock()

    async def open(self, services: Services) -> None:
        """Open the trail in append mode, creating it and its directory.

        Raises:
            ConfigError: If ``path`` cannot be written.
        """
        self._writer = _append(self.options.path)
        self._events = services.events

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.flush()
                writer.close()
            except Exception:  # pragma: no cover - defensive
                _log.exception("audit close failed")

    @listen(AuditEvent)
    def write(self, event: AuditEvent) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            with self._lock:
                writer.write(event.to_json() + "\n")
                writer.flush()
        except Exception:  # the trail must never break a request
            _log.exception("audit write failed")
            if self._events is not None:
                self._events.notify(AuditWriteFailed())


def _append(where: str) -> TextIO:
    """The trail at ``where`` opened to append, created owner-only with its directory.

    Raises:
        ConfigError: If it cannot be written.
    """
    path = Path(os.path.expanduser(where))
    try:
        make_private_dir(path.parent)
        path.touch(mode=0o600)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        return open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by the feature
    except OSError as exc:
        raise ConfigError(
            f"cannot write features.audit.path {path}: {exc.strerror or exc}"
        ) from exc
