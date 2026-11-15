"""Encryption-based operator — reversible tokens backed by ciphertext at rest.

Like :class:`~privyx.privacy.operator.hash.HashOperator`, this replaces each
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
:class:`~privyx.core.errors.ConfigError`, never mid-request.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator, free_token, restore
from privyx.privacy.transform.text import apply_replacements
from privyx.security.keys import derive
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.token.model import LogicalToken

#: Default hex characters of the keyed identifier digest to keep (64 bits —
#: matches the anchor-token convention; ``token.id_length`` overrides it).
_ID_LENGTH = 16


class EncryptOperator(BaseOperator):
    """Replace sensitive spans with tokens whose mapping value is ciphertext.

    Args:
        key: 32-byte AES-256 key (build it from config via
            :func:`privyx.security.keys.load_key`).
        codec: Token codec deciding how tokens are serialized.  Defaults to the
            built-in :meth:`~privyx.token.codec.FormatCodec.default` syntax.
        id_length: Hex characters of the identifier digest kept in the token;
            a collision makes it longer.

    Raises:
        ConfigError: If the ``cryptography`` package is not installed.
    """

    name = "encrypt"

    def __init__(
        self, key: bytes, codec: TokenCodec | None = None, id_length: int = _ID_LENGTH
    ) -> None:
        try:
            from privyx.security.crypto import DeterministicCipher
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ConfigError(
                "operator type 'encrypt' requires the 'crypto' extra; install with "
                "`pip install privyx[crypto]` (or `uv sync --all-extras`)"
            ) from exc
        self._key = key
        self._cipher = DeterministicCipher(key)
        self._codec = codec or FormatCodec.default()
        self._id_length = id_length

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
            transforms.append(Transformation(span.start, span.end, span.text, token))
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
