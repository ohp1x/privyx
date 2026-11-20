"""Encryption-based operator — reversible tokens backed by ciphertext at rest.

Like :class:`~privyx.operators.HashOperator`, this replaces each
detected span with a codec token and relies on the session vault to reverse it.
The difference is *what the vault stores*: ``hash``/``pseudonym`` keep the
plaintext there, while ``encrypt`` keeps the **ciphertext** and decrypts on
restore.  So a leaked or shared vault exposes no plaintext PII — the gap
``docs/security/cryptography.md`` calls out ("v0.1 stores session mappings in
plaintext … production deployments should enable at-rest encryption").

Both the token identifier and the AES-256-GCM ciphertext are deterministic
(derived from the value under the key), so the same value maps to the same token
every time — dedup and idempotent writes, exactly like the other reversible
operators.  Reversal is authenticated: a corrupted or foreign token fails to
decrypt and is left untouched.

Streaming needs no special path: the output is ordinary codec tokens
(``stream_restore = "token"``), and :meth:`build_resolver` gives the streaming
recognizer the same decrypting lookup that batch restore uses, so ``stream ==
batch`` holds by construction.

``encrypt`` is an optional extra; selecting it without the ``cryptography``
package installed (or without a key) fails at startup with a
:class:`~privyx.core.ConfigError`, never mid-request.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from privyx.core import (
    ConfigError,
    Context,
    Detection,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._base import Operator
from privyx.operators._restore import free_token, restore
from privyx.security.keys import derive, load_key
from privyx.token.model import LogicalToken


class EncryptOperator(Operator):
    """Replace sensitive spans with tokens whose mapping value is ciphertext.

    ``key`` is the AES-256 key as 64 hex characters (``openssl rand -hex 32``).  The token
    keeps ``token.id_length`` hex characters of the keyed identifier digest; a
    collision makes it longer.

    Raises:
        ConfigError: If the key is missing or malformed, or the
            ``cryptography`` package is not installed.
    """

    name = "encrypt"
    uses_anchor = False

    class Options(Operator.Options):
        key: str = ""  # 64 hex chars (`openssl rand -hex 32`)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        try:
            from privyx.security.crypto import DeterministicCipher
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ConfigError(
                "operator type 'encrypt' requires the 'crypto' extra; install with "
                "`pip install privyx[crypto]` (or `uv sync --all-extras`)"
            ) from exc
        self._key = load_key(self.options.key)
        self._cipher = DeterministicCipher(self._key)

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)
        transforms: list[Transformation] = []
        for span in detection.spans:
            ciphertext = self._cipher.encrypt(span.text)
            token = free_token(
                session,
                ciphertext,
                derive(self._key, "id", f"{span.entity_type}:{span.text}").hex().upper(),
                self._id_length,
                partial(self._encode, span.entity_type),
            )
            session.put(token, ciphertext)
            transforms.append(
                Transformation(span.start, span.end, span.text, token, span.entity_type)
            )
        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        """Restore tokens this session issued by decrypting their mapping values.

        Uses the shared right-to-left :func:`restore` with a decrypting resolver,
        so the batch path recognizes tokens exactly as the streaming path does.
        A token the session never issued — or whose value fails the GCM tag — is
        left as-is.
        """
        return restore(text, session, self._codec, resolve=self.build_resolver(session.mapping))

    def build_resolver(self, mapping: dict[str, str]) -> Callable[[str], str | None]:
        """Return ``token → plaintext`` by decrypting the ciphertext in ``mapping``."""

        def resolve(token: str) -> str | None:
            stored = mapping.get(token)
            return None if stored is None else self._cipher.decrypt(stored)

        return resolve

    def _encode(self, entity_type: str, identifier: str) -> str:
        """Mint the codec token for ``identifier``.

        The identifier is a keyed digest of the value, so it is deterministic
        (same value ⇒ same token ⇒ idempotent vault write) yet unguessable
        without the key.
        """
        return self._codec.encode(
            LogicalToken(
                namespace=self._codec.namespace,
                type=entity_type,
                identifier=identifier,
            )
        )
