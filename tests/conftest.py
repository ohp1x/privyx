"""Keep the suite out of the developer's own home directory.

Privyx reads ``~/.privyx/config.yaml`` and writes its database, audit trail and
anchor secret next to it, and the CLI moves files there from older locations.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_HOME_VARS = ("HOME", "USERPROFILE")

# Set while this file is imported: some test modules load settings as they are
# collected, before any fixture runs.
_session_home = tempfile.mkdtemp(prefix="privyx-tests-")
for _var in _HOME_VARS:
    os.environ[_var] = _session_home
for _var in ("PRIVYX_CONFIG", "PRIVYX_NO_DISCOVERY", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty home directory for each test."""
    path = tmp_path_factory.mktemp("home")
    for var in _HOME_VARS:
        monkeypatch.setenv(var, str(path))
    return path
