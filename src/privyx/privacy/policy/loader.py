"""Policy loader that builds policies from configuration."""

from __future__ import annotations

from typing import Any

from privyx.core.errors import ConfigError
from privyx.plugins.registry import PLUGINS
from privyx.privacy.policy.base import Policy
from privyx.privacy.policy.default import DefaultPolicy
from privyx.privacy.policy.strict import StrictPolicy


def build_policy(config: dict[str, Any] | None = None) -> Policy:
    """Build a policy from a config dict.

    Supported:
        - ``{"type": "default"}`` or ``None`` → DefaultPolicy
        - ``{"type": "strict", "allowed": ["EMAIL", "PHONE"]}`` → StrictPolicy
    """
    if config is None:
        return DefaultPolicy()
    if not isinstance(config, dict):
        raise ConfigError(f"invalid policy config: {config!r}")
    ptype = config.get("type", "default")
    if ptype == "default":
        return DefaultPolicy()
    if ptype == "strict":
        allowed = set(config.get("allowed", [])) or None
        return StrictPolicy(allowed)
    if ptype in PLUGINS.policies:
        return PLUGINS.policies.build(config)
    raise ConfigError(f"unknown policy type: {ptype}")
