"""Faker-based operator — realistic fake values instead of tokens.

Unlike :class:`~privyx.operators.PseudonymOperator`, which
substitutes a syntactically distinctive token (``<PRIVYX_EMAIL_1>``), this
operator replaces each detected span with a *realistic* fake of the same kind: a
fake email for an ``EMAIL``, a fake name for a ``PERSON``.  Keeping the shape of
the data lets a model reason over it naturally.

That realism has a cost the token approach does not pay — a fake value is
ordinary text with no delimiter — and it shows up in two places, both documented
on the methods below:

* **Literal restore.**  Reversal matches the *exact substituted strings* (via the
  session mapping), not a token grammar.  Batch and streaming share that
  matching, so ``stream == batch`` still holds; the streaming side selects a trie
  recognizer because this operator sets :attr:`stream_restore` to ``"literal"``.
* **Possible false positives.**  A fake value that also occurs naturally in a
  response can be restored by coincidence — a risk the token operators cannot
  have.  Prefer ``pseudonym`` when collision-free reversal matters more than
  realism.

``faker`` is an optional dependency; selecting this operator without it installed
fails at startup with a :class:`~privyx.core.ConfigError` rather than on
the first request (mirroring how a missing vault extra fails in the builder).
"""

from __future__ import annotations

import hashlib
from typing import Any

from privyx.core import ConfigError, Services
from privyx.operators._literal import LiteralOperator

#: Entity type (upper-cased) → the Faker method that fakes it.  Covers the
#: built-in detector's types plus the common Presidio/YAML ones; anything else
#: falls back to a neutral two-word phrase.  Dispatch is by method *name* so the
#: (untyped) Faker instance is only ever reached through ``getattr``.
_PROVIDER_METHODS: dict[str, str] = {
    "EMAIL": "ascii_email",
    "PHONE": "phone_number",
    "PHONE_NUMBER": "phone_number",
    "SSN": "ssn",
    "CREDIT_CARD": "credit_card_number",
    "IP_ADDRESS": "ipv4",
    "ZIPCODE": "postcode",
    "PERSON": "name",
    "NAME": "name",
    "LOCATION": "city",
    "ADDRESS": "street_address",
    "ORGANIZATION": "company",
    "ORG": "company",
    "COMPANY": "company",
    "URL": "url",
    "DATE": "date",
    "DATE_TIME": "iso8601",
}


class FakerOperator(LiteralOperator):
    """Replace detected spans with realistic fake values of the same kind.

    Each unique original maps to a consistent fake via the session's ``reverse``
    mapping, exactly like :class:`PseudonymOperator` — so the same value reads the
    same way throughout a conversation.  The fake is derived deterministically
    from the value (and the optional ``seed``), so a given original also fakes
    identically across sessions and runs, which keeps the operator testable.

    ``locale`` is a Faker locale (e.g. ``en_US``, ``id_ID``), Faker's default
    when empty.  ``seed`` is mixed into the per-value seed: two deployments
    with different seeds fake the same value differently; the same seed
    reproduces the same fakes.  Faker is loaded by :meth:`open`, or on first
    use.

    Raises:
        ConfigError: From :meth:`open`, if the ``faker`` package is not installed.
    """

    name = "faker"

    class Options(LiteralOperator.Options):
        seed: int | None = None  # determinism salt
        locale: str = ""  # Faker locale (empty → Faker's default)

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._seed = self.options.seed
        self._instance: Any = None

    async def open(self, services: Services) -> None:
        """Load Faker."""
        await super().open(services)
        self._loaded()

    def _loaded(self) -> Any:
        if self._instance is None:
            self._instance = _faker(self.options.locale or None, "operator type 'faker'")
        return self._instance

    def _fake_for(self, entity_type: str, original: str, salt: int = 0) -> str:
        """Deterministically fake ``original`` for ``entity_type``.

        The Faker instance is seeded from a hash of the value so the same input
        yields the same fake every time, independent of any global RNG state.
        """
        faker = self._loaded()
        _reseed(faker, self._seed, entity_type, original, salt)
        method = _PROVIDER_METHODS.get(entity_type.upper())
        if method is None:
            return f"{faker.word()}-{faker.word()}"
        return str(getattr(faker, method)())


class FakeLabels:
    """Fake values that label pseudonym tokens, for ``token.label_format``.

    Derived from the seed, the entity type, and the token, never from the
    value: the provider sees the token anyway, so a label tells it nothing
    about the value, and trying candidate values cannot find one.  A token
    gets the same label in every request, so the provider's prompt cache
    keeps working.  Only a type that has a Faker provider gets one.

    Raises:
        ConfigError: If the ``faker`` package is not installed.
    """

    def __init__(self, locale: str | None = None, seed: int | None = None) -> None:
        self._faker = _faker(locale, "token.label_format")
        self._seed = seed

    def __call__(self, entity_type: str, token: str, salt: int = 0) -> str | None:
        """A label for ``token``, another for each ``salt``; ``None`` for a type without one."""
        method = _PROVIDER_METHODS.get(entity_type.upper())
        if method is None:
            return None
        _reseed(self._faker, self._seed, entity_type, token, salt)
        return str(getattr(self._faker, method)())


def _faker(locale: str | None, what: str) -> Any:
    """A Faker instance for ``locale``; ``what`` names the setting that needs it."""
    try:
        from faker import Faker
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ConfigError(
            f"{what} requires the 'faker' extra; install with "
            "`pip install privyx[faker]` (or `uv sync --all-extras`)"
        ) from exc
    return Faker(locale) if locale else Faker()


def _reseed(faker: Any, seed: int | None, entity_type: str, original: str, salt: int) -> None:
    """Seed ``faker`` from the value, so the same input fakes the same way every time."""
    digest = hashlib.sha256(f"{seed}:{entity_type}:{original}:{salt}".encode()).hexdigest()
    faker.seed_instance(int(digest[:16], 16))
