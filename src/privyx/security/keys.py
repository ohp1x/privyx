"""Key loading and derivation utilities.

Keys enter Privyx as configuration or environment (never from code): the
``EncryptOperator`` reads a 32-byte AES-256 key from ``operator.key`` /
``PRIVYX_ENCRYPT_KEY``, encoded as 64 hex characters (``openssl rand -hex 32``).

:func:`derive` is a domain-separated HMAC so one master key can safely produce
several independent sub-values (a token identifier, a deterministic nonce) with
no interaction between them.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import stat
import tempfile
from hashlib import sha256
from pathlib import Path

from privyx.core import ConfigError

#: Required key length in bytes (AES-256 / HMAC-SHA256).
KEY_BYTES = 32


def load_key(hex_key: str) -> bytes:
    """Decode a hex-encoded key and validate its length.

    Args:
        hex_key: ``2 * KEY_BYTES`` hex characters (e.g. ``openssl rand -hex 32``).

    Raises:
        ConfigError: If the key is empty, not valid hex, or the wrong length.
            Raised at startup (from the builder) so a misconfigured key fails
            fast rather than on the first request.
    """
    if not hex_key:
        raise ConfigError(
            "operator type 'encrypt' requires a key; set operator.key or "
            "PRIVYX_ENCRYPT_KEY to 64 hex characters (`openssl rand -hex 32`)"
        )
    try:
        key = bytes.fromhex(hex_key)
    except ValueError as exc:
        raise ConfigError("operator.key must be hex-encoded (`openssl rand -hex 32`)") from exc
    if len(key) != KEY_BYTES:
        raise ConfigError(
            f"operator.key must decode to {KEY_BYTES} bytes "
            f"({2 * KEY_BYTES} hex chars), got {len(key)}"
        )
    return key


def derive(key: bytes, label: str, data: str) -> bytes:
    """Domain-separated HMAC-SHA256 of ``data`` under ``key``.

    ``label`` namespaces the derivation so, e.g., the token identifier and the
    encryption nonce derived from the same value never collide.
    """
    return hmac.new(key, f"{label}:{data}".encode(), sha256).digest()


def generate_key() -> str:
    """Return a fresh random key as 64 hex characters (for docs/tooling)."""
    import secrets

    return secrets.token_hex(KEY_BYTES)


def default_anchor_secret_path() -> Path:
    """Where the per-user anchor secret is persisted.

    ``PRIVYX_ANCHOR_KEY_PATH`` when set (the Docker image keeps it on its
    volume), else ``~/.privyx/anchor.key``.  A relative path is refused: each
    working directory would get a key of its own.
    """
    if configured := os.environ.get("PRIVYX_ANCHOR_KEY_PATH"):
        path = Path(os.path.expanduser(configured))
        if not path.is_absolute():
            raise ConfigError(f"PRIVYX_ANCHOR_KEY_PATH must be an absolute path: {configured!r}")
        return path
    return Path(os.path.expanduser("~"), ".privyx", "anchor.key")


def make_private_dir(path: Path) -> None:
    """Create ``path`` and each missing directory above it, for its owner only (0700).

    ``mkdir(mode=..., parents=True)`` gives the parents the umask's mode, so a
    first write to ``~/.privyx/tap`` left ``~/.privyx`` open to the group.
    """
    for folder in [*reversed(path.parents), path]:
        if not folder.is_dir():
            folder.mkdir(mode=0o700, exist_ok=True)


def read_or_create_anchor_secret(path: str | Path | None = None) -> str:
    """Return a persisted per-user anchor secret, creating it on first use.

    The secret (64 hex chars) lives at ``path`` (default
    :func:`default_anchor_secret_path`) with owner-only permissions, so
    pseudonyms stay stable across commands using ``anchor.secret: auto`` and
    across runs of ``privyx run``. An empty configured secret still means
    "no anchoring". Unreadable or empty files and persistence failures
    raise ``ConfigError`` rather than silently using a temporary secret.
    """
    target = Path(path) if path is not None else default_anchor_secret_path()
    return read_or_create_key(target, what="anchor secret", setting="anchor.secret")


def read_or_create_key(target: Path, *, what: str, setting: str) -> str:
    """The key at ``target``, created owner-only on first use: 64 hex characters.

    A complete key is published atomically without replacing another
    process's key.  An existing key that others may read is made owner-only.

    Raises:
        ConfigError: If it cannot be read or written, or is empty; the message
            names ``what`` it is and the ``setting`` that gives one instead.
    """
    try:
        try:
            existing = target.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            make_private_dir(target.parent)
            # A private temporary file is fully written before it becomes visible.
            fd, name = tempfile.mkstemp(dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as pending:
                    pending.write(generate_key())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass  # another process published first; read its complete key
            finally:
                temporary.unlink()
            existing = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ConfigError(
            f"cannot read or persist automatic {what}; check "
            f"{target} permissions or set {setting} explicitly"
        ) from None
    if not existing:
        raise ConfigError(
            f"automatic {what} file is empty; remove the empty file "
            f"at {target} to create a new key, or set {setting} explicitly"
        )
    if os.name == "posix":
        # A key restored from a backup or copied in may be open to others.
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(target.stat().st_mode)
            if mode & 0o077:
                target.chmod(mode & 0o700)
    return existing
