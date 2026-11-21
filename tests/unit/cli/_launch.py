"""The built-in profiles' launch specifications, as the launch tests start from them."""

from __future__ import annotations

from privyx.agents import Agent
from privyx.agents._plan import Target, profile_target
from privyx.app import Privyx
from privyx.config.defaults import DEFAULT_AGENTS
from privyx.config.schema import AgentConfig


def build_target(profile: AgentConfig) -> Target:
    """The launch specification of ``profile``, its type built with the profile's options."""
    registry = Privyx().registry(Agent)
    kind = profile.type or "env"
    return profile_target(profile, registry.build(kind, profile.model_extra or {}))


TARGETS: dict[str, Target] = {
    name: build_target(AgentConfig.model_validate(spec)) for name, spec in DEFAULT_AGENTS.items()
}
