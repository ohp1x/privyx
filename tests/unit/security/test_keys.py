"""Tests for anchor-secret provisioning (:mod:`privyx.security.keys`)."""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from privyx.core.errors import ConfigError
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


def test_an_empty_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    path.touch()
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="empty"):
        read_or_create_anchor_secret(path)
    assert path.read_bytes() == b""


def test_unwritable_location_fails_instead_of_using_a_temporary_secret(tmp_path: Path) -> None:
    # Parent is a file, so the secret's directory can never be created.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="automatic anchor secret"):
        read_or_create_anchor_secret(blocker / "anchor.key")


def test_concurrent_creation_publishes_only_a_complete_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "anchor.key"
    link = os.link
    winners: list[str] = []

    def race(source: str, destination: Path) -> None:
        assert _HEX64.fullmatch(Path(source).read_text())
        if not winners:
            # Another process wins while the first has prepared its key.
            winners.append("creating")
            winners[0] = read_or_create_anchor_secret(path)
        link(source, destination)

    monkeypatch.setattr(os, "link", race)
    secret = read_or_create_anchor_secret(path)
    assert winners == [secret]
    assert path.read_text() == secret
    assert list(tmp_path.iterdir()) == [path]


def test_publish_failure_reports_no_secret_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("fake-sensitive-value")

    monkeypatch.setattr(os, "link", refuse)
    with pytest.raises(ConfigError, match="automatic anchor secret") as exc:
        read_or_create_anchor_secret(tmp_path / "anchor.key")
    assert "fake-sensitive-value" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_invalid_existing_secret_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="automatic anchor secret"):
        read_or_create_anchor_secret(path)
    assert path.read_bytes() == b"\xff"
