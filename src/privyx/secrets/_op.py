"""``op://vault/item/field``: a 1Password item, read with the ``op`` CLI."""

from __future__ import annotations

import shutil
import subprocess

from privyx.core import ConfigError
from privyx.secrets._base import SecretResolver


class OnePasswordResolver(SecretResolver):
    """Reads ``op://vault/item/field`` with ``op read``, which may ask to unlock the app."""

    name = "op"

    def resolve(self, reference: str, path: str) -> str:
        if shutil.which("op") is None:
            raise ConfigError(f"{path} names a 1Password secret, but the `op` CLI is not installed")
        try:
            done = subprocess.run(
                ["op", "read", "--no-newline", reference],
                capture_output=True,
                text=True,
                timeout=120,  # long enough to unlock the app
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ConfigError(f"`op read` timed out for {path}") from None
        if done.returncode != 0:
            reason = done.stderr.strip().splitlines()[-1:] or ["no output"]
            raise ConfigError(f"`op read` failed for {path}: {reason[0]}")
        return done.stdout
