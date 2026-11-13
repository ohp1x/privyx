"""Key loading and derivation utilities.

Keys enter Privyx as configuration or environment (never from code): the
``EncryptOperator`` reads a 32-byte AES-256 key from ``operator.key`` /
``PRIVYX_ENCRYPT_KEY``, encoded as 64 hex characters (``openssl rand -hex 32``).

:func:`derive` is a domain-separated HMAC so one master key can safely produce
several independent sub-values (a token identifier, a deterministic nonce) with
no interaction between them.
"""

from __future__ import annotations

import hmac
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from privyx.core.errors import ConfigError

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
    """Where a per-user anchor secret is persisted by default: ``~/.privyx/anchor.key``."""
    return Path(os.path.expanduser("~"), ".privyx", "anchor.key")


def read_or_create_anchor_secret(path: str | Path | None = None) -> str:
    """Return a persisted per-user anchor secret, creating it on first use.

    The secret (64 hex chars) lives at ``path`` (default
    :func:`default_anchor_secret_path`) with owner-only permissions, so
    pseudonyms stay stable across commands using ``anchor.secret: auto`` and
    across runs of ``privyx run``. An empty configured secret still means
    "no anchoring". A complete key is published atomically without replacing
    another process's key. Unreadable or empty files and persistence failures
    raise ``ConfigError`` rather than silently using a temporary secret.
    """
    target = Path(path) if path is not None else default_anchor_secret_path()
    try:
        try:
            existing = target.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            target.parent.mkdir(parents=True, exist_ok=True)
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
            "cannot read or persist automatic anchor secret; check "
            "~/.privyx/anchor.key permissions or set anchor.secret explicitly"
        ) from None
    if not existing:
        raise ConfigError(
            "automatic anchor secret file is empty; remove the empty file "
            "at ~/.privyx/anchor.key to create a new key, or set anchor.secret explicitly"
        )
    return existing
