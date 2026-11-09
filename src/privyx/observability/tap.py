"""Developer tap: each masked exchange before and after masking.

Opt-in (``audit.tap``) and separate from the audit trail, which never holds
content.  The tap does: per session, one JSONL file in ``audit.tap_dir`` gets
two records per exchange::

    {"stage": "request", "before": <client body>, "after": <body sent upstream>, ...}
    {"stage": "response", "upstream": <upstream body>, "restored": <body sent back>, ...}

A batch body is kept as parsed JSON, a stream as its SSE text.  Both records
share the envelope of an audit event (``time``, ``ts``, ``request_id``,
``session_id``).

The files hold original values, so the directory is ``0700`` and each file
``0600``, as for the vault.  Writing never raises into the request path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from privyx.observability.audit import _iso

_log = logging.getLogger("privyx")
#: A session id that is safe as a file name as it is; any other is hashed.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def tap_file(directory: Path, session_id: str) -> Path:
    """The tap file of ``session_id`` in ``directory``.

    A session id can come from the client (``x-privyx-session``), so one that
    is not a plain name is hashed rather than used as a path.  A hashed name
    starts with ``_``, which a plain name cannot, so no session can pick it.
    Readers still check each record's ``session_id``: a case-insensitive file
    system gives ``ses_A`` and ``ses_a`` one file.
    """
    name = session_id if _SAFE_ID.fullmatch(session_id) else _hashed(session_id)
    return directory / f"{name}.jsonl"


def _hashed(session_id: str) -> str:
    return "_" + hashlib.sha256(session_id.encode()).hexdigest()[:32]


class Tap:
    """Append tap records to one owner-only JSONL file per session."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(os.path.expanduser(str(directory)))
        self._lock = threading.Lock()

    def record(self, session_id: str, request_id: str, stage: str, **fields: Any) -> None:
        """Write one record.  Never raises into the caller."""
        now = time.time()
        line = json.dumps(
            {
                "time": _iso(now),
                "ts": now,
                "request_id": request_id,
                "session_id": session_id,
                "stage": stage,
                **fields,
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            with self._lock:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = tap_file(self.directory, session_id)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                if os.name == "posix":
                    with contextlib.suppress(OSError):
                        os.chmod(self.directory, 0o700)
                        os.chmod(path, 0o600)
        except Exception:  # the tap must never break the request path
            _log.exception("tap write failed")
