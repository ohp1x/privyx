"""Structured application logging for Privyx.

Two formats, selected by ``settings.logging``:

- ``text`` — human-readable single lines (the default).
- ``json`` — one JSON object per line, built with :func:`json.dumps` so a
  message containing quotes or newlines cannot corrupt the record.

This configures the *application* log (startup, errors, request lines) on the
root logger, which the ``privyx`` logger hierarchy inherits, plus an optional
``log_file`` that also captures third-party debug output.  The audit trail is
a separate concern with its own file — see :mod:`privyx.observability.audit`.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path
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


#: httpcore brackets every step with ``.started``/``.complete``; these carry the
#: information (new connection + host, response status + headers).  ``.failed``
#: events are always kept.
_HTTPCORE_KEEP = {"connect_tcp.started", "receive_response_headers.complete"}


def _tidy_third_party(record: logging.LogRecord) -> bool | logging.LogRecord:
    """Cut the log file's third-party debug chatter down to the lines worth reading."""
    if record.name == "aiosqlite":
        if record.msg == "operation %s completed":
            return False  # repeats the "executing" line
        if record.msg == "executing %s" and isinstance(record.args, tuple):
            # args[0] is functools.partial(conn.execute, sql, params); show just those.
            sql, *params = getattr(record.args[0], "args", None) or (None,)
            if not isinstance(sql, str):
                return False  # fetchone / close / commit carry no SQL
            tidy = copy.copy(record)
            tidy.msg = " ".join([" ".join(sql.split()), *(repr(p) for p in params if p)])
            tidy.args = ()
            return tidy
    elif record.name.startswith("httpcore."):
        event = record.getMessage().split(" ", 1)[0]
        return event in _HTTPCORE_KEEP or event.endswith(".failed")
    return True


def configure_logging(settings: Settings) -> None:
    """Set up application logging from Privyx settings.

    ``log_level`` governs Privyx's own loggers.  Third-party libraries
    (aiosqlite, httpcore, ...) reach the console only at WARNING and above; set
    ``log_file`` to capture them in full at ``log_level`` for debugging.

    Idempotent: re-configuring (``privyx run`` then ``privyx proxy`` in the same
    process, or repeated test setup) replaces Privyx's handlers rather than
    stacking a second set.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if settings.logging == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    own = logging.Filter("privyx")
    console.addFilter(lambda record: record.levelno >= logging.WARNING or own.filter(record))
    handlers: list[logging.Handler] = [console]
    if settings.log_file:
        # Third-party debug output includes raw vault rows (original PII): owner-only.
        Path(settings.log_file).touch(mode=0o600)
        log_file = logging.FileHandler(settings.log_file, encoding="utf-8")
        log_file.addFilter(_tidy_third_party)
        handlers.append(log_file)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
            existing.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.set_name(_HANDLER_NAME)
        root.addHandler(handler)
    # Without a file nobody sees third-party debug records, so don't create them.
    root.setLevel(level if settings.log_file else max(level, logging.WARNING))
    logging.getLogger("privyx").setLevel(level)
