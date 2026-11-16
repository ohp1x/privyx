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

import contextlib
import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.core.errors import ConfigError
from privyx.observability.audit import AuditLogger
from privyx.plugins.registry import (
    ANCHOR_KEY,
    CODEC_KEY,
    ID_LENGTH_KEY,
    PLUGINS,
    Registry,
    plugin_section,
)
from privyx.privacy.anchor.base import Anchor
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.detector.base import Detector
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.operator.base import Operator
from privyx.privacy.operator.encrypt import EncryptOperator
from privyx.privacy.operator.faker import FakeLabels, FakerOperator
from privyx.privacy.operator.fpe import FPEOperator
from privyx.privacy.operator.hash import HashOperator
from privyx.privacy.operator.partial import PartialOperator
from privyx.privacy.operator.pseudonym import ANCHOR_TOKEN_LENGTH, PseudonymOperator
from privyx.privacy.operator.redact import RedactOperator
from privyx.privacy.policy.base import Policy
from privyx.privacy.policy.loader import build_policy
from privyx.security.keys import load_key
from privyx.token.codec import CompactCodec, FormatCodec, LabelCodec, TokenCodec
from privyx.vault.base import Vault
from privyx.vault.memory import MemoryVault

#: Called on shutdown to release vault resources (DB handles, pools).
Closer = Callable[[], Awaitable[None]]


def _operator_registry() -> Registry[Operator]:
    registry: Registry[Operator] = Registry("operator")
    registry.register(
        "pseudonym",
        lambda cfg: PseudonymOperator(
            codec=cfg.get(CODEC_KEY),
            anchor=cfg.get(ANCHOR_KEY),
            id_length=cfg.get(ID_LENGTH_KEY, ANCHOR_TOKEN_LENGTH),
            labels=_labels(cfg),
        ),
    )
    registry.register("redact", lambda cfg: RedactOperator(cfg.get("token", "[REDACTED]")))
    registry.register(
        "hash",
        lambda cfg: HashOperator(length=int(cfg.get("length", 12)), codec=cfg.get(CODEC_KEY)),
    )
    registry.register(
        "faker",
        lambda cfg: FakerOperator(locale=cfg.get("locale") or None, seed=cfg.get("seed")),
    )
    registry.register("partial", lambda cfg: PartialOperator())
    registry.register("fpe", lambda cfg: FPEOperator(seed=cfg.get("seed")))
    registry.register(
        "encrypt",
        lambda cfg: EncryptOperator(
            key=load_key(cfg.get("key", "")),
            codec=cfg.get(CODEC_KEY),
            id_length=cfg.get(ID_LENGTH_KEY, ANCHOR_TOKEN_LENGTH),
        ),
    )
    return registry


def _labels(cfg: dict[str, Any]) -> FakeLabels | None:
    """The labels of a ``pseudonym`` operator whose codec writes them."""
    if not isinstance(cfg.get(CODEC_KEY), LabelCodec):
        return None
    return FakeLabels(locale=cfg.get("locale") or None, seed=cfg.get("seed"))


OPERATORS = _operator_registry()


def build_codec(settings: Settings) -> TokenCodec:
    """Build the configured token codec.

    Raises:
        TokenFormatError: If ``token.format`` or ``token.label_format`` is
            unusable (a ``ConfigError``, so an invalid syntax fails at startup
            rather than on the first request).
        ConfigError: If ``token.label_format`` is set with another operator
            than ``pseudonym``, or with ``token.compact``.
    """
    token = settings.token
    if token.label_format:
        if token.compact:
            raise ConfigError("token.label_format cannot be combined with token.compact")
        if settings.operator.type != "pseudonym":
            raise ConfigError(
                f"token.label_format needs operator.type: pseudonym, not {settings.operator.type!r}"
            )
        return LabelCodec(token.format, token.label_format, token.namespace)
    codec = FormatCodec(token.format, token.namespace)
    return CompactCodec(codec) if token.compact else codec


def build_operator(
    config: dict[str, Any] | None = None,
    *,
    anchor: Anchor | None = None,
    codec: TokenCodec | None = None,
    id_length: int | None = None,
) -> Operator:
    """Build the operator named by ``config["type"]`` (default ``pseudonym``).

    Args:
        config: The ``operator:`` section of the config.
        anchor: Optional anchor, handed to operators that can use one.
            Operators that cannot simply ignore it.
        codec: Optional token codec, handed to operators that emit tokens.
            Operators that do not (e.g. ``redact``) ignore it.
        id_length: Optional ``token.id_length``, for anchored ``pseudonym``
            and ``encrypt`` ids.

    ``faker`` and ``encrypt`` need optional extras — selecting one without its
    package installed (``faker``; ``cryptography`` for ``encrypt``) fails at
    startup with a ``ConfigError`` raised from the operator, not
    ``NotImplementedError`` mid-request.  ``encrypt`` additionally requires a key:
    an empty ``operator.key`` fails fast in :func:`~privyx.security.keys.load_key`.
    """
    config = config or {}
    if anchor is not None:
        config = {**config, ANCHOR_KEY: anchor}
    if codec is not None:
        config = {**config, CODEC_KEY: codec}
    if id_length is not None:
        config = {**config, ID_LENGTH_KEY: id_length}
    otype = config.get("type", "pseudonym")
    if otype not in OPERATORS and otype in PLUGINS.operators:
        return PLUGINS.operators.build(config)
    return OPERATORS.build(config, default="pseudonym")


def build_anchor(settings: Settings) -> Anchor | None:
    """Build the configured anchor, or ``None`` when HMAC anchoring is off.

    HMAC anchoring only takes effect once a secret is set: without a key, an
    HMAC anchor would derive pseudonyms from an empty string — deterministic,
    but trivially reproducible by anyone, which defeats the point.  So an empty
    ``anchor.secret`` means "no anchoring" rather than "anchoring with a
    guessable key".  This gate is specific to ``hmac``; a plugin anchor decides
    for itself whether it needs a secret.

    ``auto`` uses the persisted per-user key, shared with ``privyx run``.
    Settings retain the marker so inspection cannot expose that key.

    Raises:
        ConfigError: If ``anchor.type`` is unknown or the automatic key cannot
            be read or persisted.
    """
    atype = settings.anchor.type
    secret = settings.anchor.secret
    if secret == "auto" and (atype == "hmac" or atype in PLUGINS.anchors):
        from privyx.security.keys import read_or_create_anchor_secret

        secret = read_or_create_anchor_secret()
    if atype == "hmac":
        if not secret:
            return None
        return HMACAnchor(secret)
    if atype == "pasp":
        raise ConfigError("anchor type 'pasp' is not implemented yet; use 'hmac'")
    if atype in PLUGINS.anchors:
        return PLUGINS.anchors.build({**plugin_section(settings.anchor), "secret": secret})
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
        return MemoryVault(ttl=settings.vault.ttl), _noop

    if vault_type == "sqlite":
        from privyx.vault.sqlite import SQLiteVault

        vault = SQLiteVault(_sqlite_path(settings.vault.dsn), ttl=settings.vault.ttl)
        await vault.connect()
        return vault, vault.close

    if vault_type == "redis":
        try:
            from privyx.vault.redis import RedisVault, redis_client
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigError(
                "redis vault requires the redis extra: pip install privyx[redis]"
            ) from exc

        client = redis_client(settings.vault.redis_url)
        return RedisVault(client, ttl=settings.vault.ttl), client.aclose

    if vault_type in PLUGINS.vaults:
        plugin_vault = PLUGINS.vaults.build(plugin_section(settings.vault))
        # A plugin vault may need an async handshake and may own resources to
        # release; call ``connect``/``close`` if present, mirroring SQLiteVault.
        connect = getattr(plugin_vault, "connect", None)
        if callable(connect):
            result = connect()
            if inspect.isawaitable(result):
                await result
        close = getattr(plugin_vault, "close", None)
        closer: Closer = close if callable(close) else _noop
        return plugin_vault, closer

    raise ConfigError(f"unknown vault type: {vault_type!r}")


def _sqlite_path(dsn: str) -> str:
    """Accept both a bare path and a SQLAlchemy-style URL; ``~`` is the home directory."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if dsn.startswith(prefix):
            dsn = dsn[len(prefix) :]
            break
    return os.path.expanduser(dsn)


def build_audit_logger(settings: Settings) -> AuditLogger:
    """Build the configured audit logger.

    When ``audit.enabled`` is set, opens ``audit.path`` in append mode, creating
    its directory, and hands the handle to an :class:`AuditLogger`; the caller
    owns it and must call :meth:`AuditLogger.close` on shutdown.  When disabled,
    returns a no-op logger (a ``None`` writer), so call sites need no
    ``if audit:`` guards.

    Raises:
        ConfigError: If ``audit.path`` cannot be written.
    """
    if not settings.audit.enabled:
        return AuditLogger(None)
    path = Path(os.path.expanduser(settings.audit.path))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Session ids and the upstream host: a new file is owner-only.
        path.touch(mode=0o600)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        writer = open(path, "a", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot write audit.path {path}: {exc.strerror or exc}") from exc
    return AuditLogger(writer)


def build_detector_from(settings: Settings) -> Detector:
    """Build the configured detector (a composite when a list is configured)."""
    if isinstance(settings.detector, list):
        return build_detector([plugin_section(d) for d in settings.detector])
    return build_detector(plugin_section(settings.detector))


def build_policy_from(settings: Settings) -> Policy:
    """Build the configured policy."""
    return build_policy(plugin_section(settings.policy))


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
        ConfigError: If a component's config names an unknown type or the
            automatic anchor key cannot be read or persisted.
    """
    anchor = build_anchor(settings)
    vault, closer = await build_vault(settings)
    codec = build_codec(settings)
    engine = PrivacyEngine(
        detector=build_detector_from(settings),
        policy=build_policy_from(settings),
        operator=build_operator(
            plugin_section(settings.operator),
            anchor=anchor,
            codec=codec,
            id_length=settings.token.id_length,
        ),
        vault=vault,
        codec=codec,
        audit=audit,
        dlp_check=settings.proxy.dlp_check,
        directive=settings.proxy.directive,
        cache_control=settings.proxy.cache_control,
        underscore_separates_words=any(
            d.underscore_separates_words
            for d in (
                settings.detector if isinstance(settings.detector, list) else [settings.detector]
            )
        ),
    )
    return engine, closer
