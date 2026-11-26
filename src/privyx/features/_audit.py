"""The ``audit`` feature: the audit trail, one JSON line per notification.

Each line is written and flushed as the notification is emitted, never
queued, and not synced to disk.  A line that cannot be written, on a full
disk, is lost and the request goes on: the failure is logged and notified
(:class:`~privyx.events.AuditWriteFailed`, which ``GET /metrics`` counts).  The
file is created owner-only: it names sessions and the upstream host, never a
value.

With ``conversation_ref``, each ``proxy.request`` also carries a reference to
the agent conversation the request belongs to: a keyed hash of the ID Claude
Code or Codex sends, never the ID, which ``privyx audit find --conversation -``
computes again to find the conversation's records.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any, TextIO

from pydantic import Field

from privyx.config.schema import ModelPrice
from privyx.core import ConfigError, EventBus, Services, listen
from privyx.events import (
    AuditEvent,
    AuditWriteFailed,
    ProxyRequest,
    RequestEnded,
    RequestReceived,
)
from privyx.features._base import Feature
from privyx.security.keys import make_private_dir, read_or_create_key

_log = logging.getLogger("privyx")


class AuditTrail(Feature):
    """Appends each audit notification to ``path`` as a JSON line."""

    name = "audit"
    enabled_by_default = True

    class Options(Feature.Options):
        path: str = "~/.privyx/audit.log"
        #: Model-id prefix -> USD per million tokens, for ``privyx session show``.
        prices: dict[str, ModelPrice] = Field(default_factory=dict)
        #: Add ``conversation_ref`` to each ``proxy.request`` of Claude Code or Codex.
        conversation_ref: bool = False
        #: The key of ``conversation_ref``: ``auto`` keeps one in a file beside ``path``.
        key: str = "auto"

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._writer: TextIO | None = None
        self._events: EventBus | None = None
        # One write() per line, so lines of concurrent requests never mix.
        self._lock = threading.Lock()
        self._key: bytes | None = None
        #: The reference of each request under way, by request id.
        self._refs: dict[str, str] = {}
        self._added: list[Any] = []

    async def open(self, services: Services) -> None:
        """Open the trail in append mode, creating it and its directory.

        With ``conversation_ref``, read the key, created on first use for
        ``auto``, and hear each request as it arrives: only then, so that
        no request waits on it otherwise.

        Raises:
            ConfigError: If ``path`` cannot be written, or the key read.
        """
        if self.options.conversation_ref:
            self._key = trail_key(self.options, create=True)
        self._writer = _append(self.options.path)
        self._events = services.events
        if self._key is not None:
            self._added = [
                services.events.add(RequestReceived, self._received),
                services.events.add(RequestEnded, self._ended),
            ]

    async def close(self) -> None:
        added, self._added = self._added, []
        if self._events is not None:
            for entry in added:
                self._events.discard(entry)
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.flush()
                writer.close()
            except Exception:  # pragma: no cover - defensive
                _log.exception("audit close failed")

    def _received(self, event: RequestReceived) -> None:
        if event.conversation is not None and self._key is not None:
            self._refs[event.request_id] = conversation_ref(self._key, *event.conversation)

    def _ended(self, event: RequestEnded) -> None:
        self._refs.pop(event.request_id, None)

    @listen(AuditEvent)
    def write(self, event: AuditEvent) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            ref = isinstance(event, ProxyRequest) and self._refs.get(event.request_id or "")
            line = (
                json.dumps(
                    {**event.to_dict(), "conversation_ref": ref}, ensure_ascii=False, default=str
                )
                if ref
                else event.to_json()
            )
            with self._lock:
                writer.write(line + "\n")
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


def trail_key(
    options: AuditTrail.Options, path: Path | None = None, *, create: bool = False
) -> bytes:
    """The key of ``conversation_ref`` for the trail at ``path``, ``options.path`` by default.

    ``auto`` is the key in the file beside the trail (``audit.log.key``),
    created owner-only when ``create`` is set; one key per trail, so a
    reference lives as long as the trail that holds it.

    Raises:
        ConfigError: If the key is empty, cannot be read or created, or, not
            created, is not there.
    """
    if options.key != "auto":
        if not options.key:
            raise ConfigError("features.audit.key is empty; set it, or use auto")
        return options.key.encode()
    trail = path or Path(os.path.expanduser(options.path))
    target = trail.with_name(trail.name + ".key")
    if not create and not target.exists():
        raise ConfigError(
            f"no conversation_ref key beside {trail}: the trail holds no "
            "conversation_ref unless features.audit.conversation_ref is on"
        )
    return read_or_create_key(target, what="audit key", setting="features.audit.key").encode()


def conversation_ref(key: bytes, agent: str, native: str) -> str:
    """The reference to the conversation of ``agent`` that ``native`` names.

    ``c1.``, the key's id, and 32 hex characters of an HMAC-SHA256 under
    ``key`` of the format's version, the agent type, the caller (empty until
    Privyx knows its callers), and the ID, each led by its length so that no
    two run together.
    """
    fields = ("1", agent, "", native)
    data = "".join(f"{len(field)}:{field}" for field in fields).encode()
    return f"c1.{key_id(key)}.{hmac.new(key, data, sha256).hexdigest()[:32]}"


def key_id(key: bytes) -> str:
    """Eight hex characters that tell one key from another, and reveal nothing of it."""
    return hmac.new(key, b"privyx audit key id", sha256).hexdigest()[:8]
