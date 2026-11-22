"""Build the components a config names, and the :class:`PrivacyEngine` from them.

This is the seam between configuration and the privacy pipeline: each
section's ``type`` is looked up in the app's registry, the component is built
with the section's options, and :meth:`~privyx.core.Component.open` gives it
what the app shares.  The engine itself stays transport-agnostic.

Example::

    settings = load_config("configs/strict.yaml")
    engine, close = await build_engine(settings)
    try:
        result = await engine.transform("email alice@example.com")
    finally:
        await close()
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from privyx.anchors import Anchor
from privyx.config.schema import DetectorConfig, PluggableConfig, Settings
from privyx.core import Component, ConfigError, Services
from privyx.detectors import Detector
from privyx.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.operators import Operator
from privyx.policies import Policy
from privyx.providers import Provider, check_credential, resolve_api_key, resolve_base_url
from privyx.token import FormatCodec
from privyx.token import build_codec as _build_codec
from privyx.vaults import Vault

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx

#: Called on shutdown to close what was opened.
Closer = Callable[[], Awaitable[None]]


def build_codec(settings: Settings, app: Privyx | None = None) -> FormatCodec:
    """Build the configured token codec.

    Raises:
        TokenFormatError: If ``token.format`` or ``token.label_format`` is
            unusable (a ``ConfigError``, so an invalid syntax fails at startup
            rather than on the first request).
        ConfigError: If ``token.label_format`` is set with an operator that
            does not write labels, or with ``token.compact``.
    """
    from privyx.operators import PseudonymOperator

    token = settings.token
    if token.label_format and not token.compact:
        operator = _app(app).registry(Operator).get(settings.operator.type)
        if not issubclass(operator, PseudonymOperator):
            raise ConfigError(
                f"token.label_format needs operator.type: pseudonym, not {settings.operator.type!r}"
            )
    return _build_codec(
        token.format, token.namespace, compact=token.compact, label_format=token.label_format
    )


def build_operator(settings: Settings, app: Privyx | None = None) -> Operator:
    """Build the configured operator, not yet opened: it writes the built-in token syntax.

    ``faker`` and ``encrypt`` need optional extras; selecting one without its
    package installed fails at startup with a ``ConfigError``.  ``encrypt``
    additionally requires a key.
    """
    return _build(_app(app), Operator, settings.operator, "operator")


async def open_operator(settings: Settings, app: Privyx | None = None) -> Operator:
    """:func:`build_operator`, opened with the configured codec and anchor, for one-shot use.

    Raises:
        ConfigError: As :func:`build_operator`, :func:`build_anchor`, and
            :func:`build_codec`.
    """
    app = _app(app)
    anchor = build_anchor(settings, app)
    operator = build_operator(settings, app)
    services = _services(app, build_codec(settings, app), anchor)
    if anchor is not None:
        await anchor.open(services)
    await operator.open(services)
    return operator


def build_anchor(settings: Settings, app: Privyx | None = None) -> Anchor | None:
    """Build the configured anchor, not yet opened, or ``None`` when there is none.

    An anchor whose type takes a ``secret`` is off when the secret is empty:
    a pseudonym derived with an empty key would be trivially reproducible by
    anyone.  ``auto``, the per-user key, is read by the anchor when it opens,
    and only for an operator that can use an anchor.

    Raises:
        ConfigError: If ``anchor.type`` is unknown or its options invalid.
    """
    app = _app(app)
    operator = app.registry(Operator).get(settings.operator.type)
    section = settings.anchor
    secret = _options(section).get("secret", "auto")
    if secret == "auto" and not operator.uses_anchor:
        return None  # no key to read for an operator that never uses it
    anchor = _build(app, Anchor, section, "anchor")
    return None if secret == "" and "secret" in type(anchor).Options.model_fields else anchor


def build_vault(settings: Settings, app: Privyx | None = None) -> Vault:
    """Build the configured vault, not yet opened.

    Raises:
        ConfigError: If the vault type is unknown or its options invalid.
    """
    return _build(_app(app), Vault, settings.vault, "vault")


async def open_vault(settings: Settings, app: Privyx | None = None) -> tuple[Vault, Closer]:
    """:func:`build_vault`, opened, and a coroutine that closes it.

    Raises:
        ConfigError: As :func:`build_vault`, or if its optional extra is not installed.
        VaultError: If it cannot be opened.
    """
    app = _app(app)
    vault = build_vault(settings, app)
    await vault.open(_services(app, FormatCodec.default(), None))
    return vault, vault.close


def build_detector(
    sections: Mapping[str, DetectorConfig | Mapping[str, Any]], app: Privyx | None = None
) -> Detector:
    """Build the detectors of ``sections``, by name, the ones that are on run together.

    An entry is a :class:`DetectorConfig` or a mapping of one; its type is its
    name's without a ``type``.  Each takes the ``cache`` it sets, so a list
    caches each detector on its own.

    Raises:
        ConfigError: If no detector is on, a type is unknown, or an option invalid.
    """
    from privyx.detectors._cache import CachedDetector
    from privyx.detectors._composite import CompositeDetector

    app = _app(app)
    detectors: list[Detector] = []
    for name, entry in sections.items():
        section = (
            entry
            if isinstance(entry, DetectorConfig)
            else DetectorConfig.model_validate({"type": name, **entry})
        )
        if not section.type:
            section = section.model_copy(update={"type": name})
        if not section.enabled:
            continue
        detector: Detector = _build(app, Detector, section, f"detectors.{name}")
        cache = section.cache
        enabled = cache if isinstance(cache, bool) else cache.enabled
        if enabled:
            size = 10000 if isinstance(cache, bool) else cache.max_size
            detector = CachedDetector(detector, max_size=size)
        detectors.append(detector)
    if not detectors:
        # No detector means nothing is ever pseudonymized — fail loudly.
        raise ConfigError("no detector is on: turn one on under 'detectors'")
    return detectors[0] if len(detectors) == 1 else CompositeDetector(detectors)


def build_policy(settings: Settings, app: Privyx | None = None) -> Policy:
    """Build the configured policy."""
    return _build(_app(app), Policy, settings.policy, "policy")


def build_provider(settings: Settings, app: Privyx | None = None) -> Provider:
    """Build the configured provider, with the upstream URL and key the settings resolve to.

    The URL follows :func:`~privyx.providers.resolve_base_url` and the key
    :func:`~privyx.providers.resolve_api_key`.

    Raises:
        ConfigError: If ``provider.type`` is unknown or its options invalid,
            or its credential would go to another origin than its own.
    """
    check_credential(settings)
    section = settings.provider
    options = {
        **section.model_dump(exclude={"type"}),
        "api_key": resolve_api_key(settings) or "",
        "base_url": resolve_base_url(settings),
    }
    return _app(app).registry(Provider).build(section.type, options, setting="provider")


async def open_provider(settings: Settings, app: Privyx | None = None) -> Provider:
    """:func:`build_provider`, opened; the caller closes it.

    Raises:
        ConfigError: If ``provider.type`` is unknown, its options invalid, or
            it cannot be opened.
    """
    app = _app(app)
    provider = build_provider(settings, app)
    await provider.open(_services(app, FormatCodec.default(), None))
    return provider


async def build_engine(
    settings: Settings, app: Privyx | None = None, *, audit: AuditLogger | None = None
) -> tuple[PrivacyEngine, Closer]:
    """Assemble a :class:`PrivacyEngine` from ``settings``, its components opened.

    Args:
        settings: Validated settings.
        app: The app whose components the config may name; without one, an
            app with the built-ins and the plugins the config loads.
        audit: Optional audit logger the engine emits privacy events to.  When
            omitted the engine gets a no-op logger, so diagnostic callers (e.g.
            ``privyx doctor``) write no audit trail.

    Returns:
        ``(engine, close)``.  ``close`` closes the components in reverse order
        and must be awaited on shutdown.

    Raises:
        ConfigError: If a component's config names an unknown type, an option
            is invalid, or a component cannot be opened.
    """
    if app is None:
        from privyx.app._plugins import load_plugins

        app = load_plugins(settings)
    anchor = build_anchor(settings, app)
    vault = build_vault(settings, app)
    codec = build_codec(settings, app)
    detector = build_detector(settings.detectors, app)
    policy = build_policy(settings, app)
    operator = build_operator(settings, app)
    services = _services(app, codec, anchor)
    opened: list[Component] = []
    try:
        for component in (vault, anchor, operator, detector, policy):
            if component is not None:
                await component.open(services)
                opened.append(component)
    except BaseException:
        await _close(opened)
        raise
    engine = PrivacyEngine(
        detector=detector,
        policy=policy,
        operator=operator,
        vault=vault,
        codec=codec,
        audit=audit,
        dlp_check=settings.proxy.dlp_check,
        underscore_separates_words=any(
            d.underscore_separates_words for d in settings.active_detectors.values()
        ),
    )

    async def close() -> None:
        await _close(opened)

    return engine, close


def _app(app: Privyx | None) -> Privyx:
    """``app``, or a new one with the built-ins."""
    from privyx.app._privyx import Privyx

    return app if app is not None else Privyx()


def _options(section: PluggableConfig) -> dict[str, Any]:
    """The options a section gives its type: every key but ``type`` and ``enabled``."""
    return section.model_dump(exclude={"type", "enabled"})


def _build[C: Component](app: Privyx, kind: type[C], section: PluggableConfig, setting: str) -> C:
    """The component of ``kind`` that ``section`` names, built with its options.

    A plugin's type does not get the options of the built-in types it does
    not take itself: a layer or the environment sets those for every type
    (``PRIVYX_VAULT_DSN`` beside a plugin vault).
    """
    registry = app.registry(kind)
    options = _options(section)
    if not registry.is_builtin(section.type):
        own = registry.get(section.type).Options.model_fields
        builtin = {
            key
            for name in registry.builtin_names()
            for key in registry.get(name).Options.model_fields
        }
        options = {key: value for key, value in options.items() if key in own or key not in builtin}
    return registry.build(section.type, options, setting=setting)


def _services(app: Privyx, codec: FormatCodec, anchor: Anchor | None) -> Services:
    """What ``app`` shares with the components it opens."""
    return Services(codec=codec, anchor=anchor, events=app._events)


async def _close(components: list[Component]) -> None:
    """Close ``components`` in reverse order."""
    for component in reversed(components):
        await component.close()
