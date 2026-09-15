"""Build a :class:`PrivacyEngine` from validated :class:`Settings`.

This is the seam between configuration and the privacy pipeline.  Without it
the CLI would have to hard-code a detector/policy/operator/vault, and every
knob in ``configs/*.yaml`` would be decorative.

The engine itself stays transport-agnostic: this module knows about config,
the engine does not.

Example::

    settings = load_config("configs/strict.yaml")
    engine, closer = await build_engine(settings)
    try:
        result = await engine.transform("email alice@example.com")
    finally:
        await closer()
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.core.errors import ConfigError
from privyx.observability.audit import AuditLogger
from privyx.plugins.registry import Registry
from privyx.privacy.anchor.base import Anchor
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.detector.base import Detector
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.operator.base import Operator
from privyx.privacy.operator.hash import HashOperator
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.operator.redact import RedactOperator
from privyx.privacy.policy.base import Policy
from privyx.privacy.policy.loader import build_policy
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.vault.base import Vault
from privyx.vault.memory import MemoryVault

#: Called on shutdown to release vault resources (DB handles, pools).
Closer = Callable[[], Awaitable[None]]


#: Keys under which :func:`build_operator` passes live objects to a factory.
#: They are not settable from YAML — the values are objects, not config — so they
#: are namespaced to make an accidental collision with a real config key obvious.
_ANCHOR_KEY = "__anchor__"
_CODEC_KEY = "__codec__"


def _operator_registry() -> Registry[Operator]:
    registry: Registry[Operator] = Registry("operator")
    registry.register(
        "pseudonym",
        lambda cfg: PseudonymOperator(codec=cfg.get(_CODEC_KEY), anchor=cfg.get(_ANCHOR_KEY)),
    )
    registry.register("redact", lambda cfg: RedactOperator(cfg.get("token", "[REDACTED]")))
    registry.register(
        "hash",
        lambda cfg: HashOperator(length=int(cfg.get("length", 12)), codec=cfg.get(_CODEC_KEY)),
    )
    return registry


OPERATORS = _operator_registry()


def build_codec(settings: Settings) -> TokenCodec:
    """Build the configured token codec.

    Raises:
        TokenFormatError: If ``token.format`` is unusable (a ``ConfigError``, so
            an invalid syntax fails at startup rather than on the first request).
    """
    return FormatCodec(settings.token.format, settings.token.namespace)


def build_operator(
    config: dict[str, Any] | None = None,
    *,
    anchor: Anchor | None = None,
    codec: TokenCodec | None = None,
) -> Operator:
    """Build the operator named by ``config["type"]`` (default ``pseudonym``).

    Args:
        config: The ``operator:`` section of the config.
        anchor: Optional anchor, handed to operators that can use one.
            Operators that cannot simply ignore it.
        codec: Optional token codec, handed to operators that emit tokens.
            Operators that do not (e.g. ``redact``) ignore it.

    ``encrypt`` and ``faker`` are intentionally absent: they are unimplemented
    placeholders, and registering them would let a config select an operator
    that raises ``NotImplementedError`` on the first request instead of failing
    at startup.
    """
    if anchor is not None:
        config = {**(config or {}), _ANCHOR_KEY: anchor}
    if codec is not None:
        config = {**(config or {}), _CODEC_KEY: codec}
    return OPERATORS.build(config, default="pseudonym")


def build_anchor(settings: Settings) -> Anchor | None:
    """Build the configured anchor, or ``None`` when anchoring is off.

    An anchor only takes effect once a secret is set: without a key, an HMAC
    anchor would derive pseudonyms from an empty string — deterministic, but
    trivially reproducible by anyone, which defeats the point.  So an empty
    ``anchor.secret`` means "no anchoring" rather than "anchoring with a
    guessable key".

    Raises:
        ConfigError: If ``anchor.type`` is unknown.
    """
    secret = settings.anchor.secret
    if not secret:
        return None
    atype = settings.anchor.type
    if atype == "hmac":
        return HMACAnchor(secret)
    if atype == "pasp":
        raise ConfigError("anchor type 'pasp' is not implemented yet; use 'hmac'")
    raise ConfigError(f"unknown anchor type: {atype!r}")


async def build_vault(settings: Settings) -> tuple[Vault, Closer]:
    """Build the configured vault and a coroutine that releases it.

    Returns:
        ``(vault, closer)``.  Always ``await closer()`` on shutdown; for
        ``memory`` it is a no-op.

    Raises:
        ConfigError: If the vault type is unknown or its optional extra is
            not installed.
    """

    async def _noop() -> None:
        return None

    vault_type = settings.vault.type
    if vault_type == "memory":
        return MemoryVault(), _noop

    if vault_type == "sqlite":
        from privyx.vault.sqlite import SQLiteVault

        vault = SQLiteVault(_sqlite_path(settings.vault.dsn))
        await vault.connect()
        return vault, vault.close

    if vault_type == "redis":
        try:
            from redis.asyncio import from_url
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigError(
                "redis vault requires the redis extra: pip install privyx[redis]"
            ) from exc
        from privyx.vault.redis import RedisVault

        client = from_url(settings.vault.redis_url)
        return RedisVault(client, ttl=settings.vault.ttl), client.aclose

    raise ConfigError(f"unknown vault type: {vault_type!r}")


def _sqlite_path(dsn: str) -> str:
    """Accept both a bare path and a SQLAlchemy-style URL."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if dsn.startswith(prefix):
            return dsn[len(prefix) :]
    return dsn


def build_audit_logger(settings: Settings) -> AuditLogger:
    """Build the configured audit logger.

    When ``audit.enabled`` is set, opens ``audit.path`` in append mode and hands
    the handle to an :class:`AuditLogger`; the caller owns it and must call
    :meth:`AuditLogger.close` on shutdown.  When disabled, returns a no-op
    logger (a ``None`` writer), so call sites need no ``if audit:`` guards.
    """
    if not settings.audit.enabled:
        return AuditLogger(None)
    writer = open(settings.audit.path, "a", encoding="utf-8")
    return AuditLogger(writer)


def build_detector_from(settings: Settings) -> Detector:
    """Build the configured detector."""
    return build_detector(settings.detector.model_dump())


def build_policy_from(settings: Settings) -> Policy:
    """Build the configured policy."""
    return build_policy(settings.policy.model_dump())


async def build_engine(
    settings: Settings, *, audit: AuditLogger | None = None
) -> tuple[PrivacyEngine, Closer]:
    """Assemble a :class:`PrivacyEngine` from ``settings``.

    Args:
        settings: Validated settings.
        audit: Optional audit logger the engine emits privacy events to.  When
            omitted the engine gets a no-op logger, so diagnostic callers (e.g.
            ``privyx doctor``) do not open or write an audit file.

    Returns:
        ``(engine, closer)``.  ``closer`` releases the vault's resources and
        must be awaited on shutdown.

    Raises:
        ConfigError: If any component's config names an unknown type.
    """
    vault, closer = await build_vault(settings)
    codec = build_codec(settings)
    engine = PrivacyEngine(
        detector=build_detector_from(settings),
        policy=build_policy_from(settings),
        operator=build_operator(
            settings.operator.model_dump(), anchor=build_anchor(settings), codec=codec
        ),
        vault=vault,
        codec=codec,
        audit=audit,
    )
    return engine, closer
