"""Tests for anchor-secret provisioning (:mod:`privyx.security.keys`)."""

from __future__ import annotations

import re
from pathlib import Path

from privyx.security.keys import read_or_create_anchor_secret

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_creates_secret_with_owner_only_perms(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    secret = read_or_create_anchor_secret(path)
    assert _HEX64.match(secret)
    assert path.read_text(encoding="utf-8").strip() == secret
    assert (path.stat().st_mode & 0o777) == 0o600


def test_reuses_existing_secret(tmp_path: Path) -> None:
    path = tmp_path / "anchor.key"
    first = read_or_create_anchor_secret(path)
    second = read_or_create_anchor_secret(path)
    assert first == second


def test_unwritable_location_still_returns_a_secret(tmp_path: Path) -> None:
    # Parent is a file, so the secret's directory can never be created.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    secret = read_or_create_anchor_secret(blocker / "anchor.key")
    assert _HEX64.match(secret)  # best effort: usable even when it can't persist
