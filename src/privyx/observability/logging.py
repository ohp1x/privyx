"""Structured application logging for Privyx.

Two formats, selected by ``settings.logging``:

- ``text`` — human-readable single lines (the default).
- ``json`` — one JSON object per line, built with :func:`json.dumps` so a
  message containing quotes or newlines cannot corrupt the record.

This configures the *application* log (startup, errors, request lines) on the
root logger, which the ``privyx`` logger hierarchy inherits.  The audit trail is
a separate concern with its own file — see :mod:`privyx.observability.audit`.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from privyx.config.schema import Settings

#: Marks the handler this module installs, so re-configuring replaces it instead
#: of stacking a second handler that double-prints every line.
_HANDLER_NAME = "privyx"


class JsonFormatter(logging.Formatter):
    """Format a log record as a single JSON object.

    Unlike a ``%``-style template with ``%(message)s`` embedded in a JSON string,
    this serializes with :func:`json.dumps`, so quotes, newlines, and unicode in
    the message are escaped rather than breaking the line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Set up application logging from Privyx settings.

    Idempotent: re-configuring (``privyx run`` then ``privyx proxy`` in the same
    process, or repeated test setup) replaces Privyx's handler rather than
    stacking a second one.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if settings.logging == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    handler.set_name(_HANDLER_NAME)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
