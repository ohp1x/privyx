"""Tests for anchor-secret provisioning (:mod:`privyx.security.keys`)."""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from privyx.security.keys import read_or_create_anchor_secret

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_creates_secret_with_owner_only_perms(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    secret = read_or_create_anchor_secret(path)
    assert _HEX64.match(secret)
    assert path.read_text(encoding="utf-8").strip() == secret
    assert (path.stat().st_mode & 0o777) == 0o600


def test_owner_only_from_creation_not_from_a_later_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("chmod refused")

    monkeypatch.setattr(os, "chmod", refuse)
    path = tmp_path / "anchor.key"
    umask = os.umask(0o022)  # a plain write would then create it 0644
    try:
        secret = read_or_create_anchor_secret(path)
    finally:
        os.umask(umask)
    assert path.read_text(encoding="utf-8") == secret
    assert (path.stat().st_mode & 0o777) == 0o600


def test_reuses_existing_secret(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    first = read_or_create_anchor_secret(path)
    second = read_or_create_anchor_secret(path)
    assert first == second


def test_first_runs_started_together_share_one_secret(tmp_path: Path) -> None:
    def first_run(path: Path, start: threading.Barrier) -> str:
        start.wait()
        return read_or_create_anchor_secret(path)

    for attempt in range(20):  # a race: give it several chances to show
        path = tmp_path / str(attempt) / "anchor.key"
        start = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = [pool.submit(first_run, path, start) for _ in range(2)]
        assert {run.result() for run in runs} == {path.read_text(encoding="utf-8")}


def test_an_empty_file_gets_a_secret_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    path.touch()
    path.chmod(0o644)
    secret = read_or_create_anchor_secret(path)
    assert _HEX64.match(secret)
    assert path.read_text(encoding="utf-8") == secret
    assert (path.stat().st_mode & 0o777) == 0o600


def test_unwritable_location_still_returns_a_secret(tmp_path: Path) -> None:
    # Parent is a file, so the secret's directory can never be created.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    secret = read_or_create_anchor_secret(blocker / "anchor.key")
    assert _HEX64.match(secret)  # best effort: usable even when it can't persist
