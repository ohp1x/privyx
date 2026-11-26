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


async def serve_target(
    spec: Target,
    argv: list[str],
    *,
    upstream: str | None = None,
    session_strategy: str | None = None,
    no_anchor: bool = False,
) -> int:
    """Run ``spec`` through a proxy as ``privyx run`` does, and report on the run.

    For a launch specification no profile names, such as a known agent's with
    a stand-in command; ``app.agents.plan`` resolves the rest from the config.
    """
    from privyx.app._run import plan_launch, run_settings
    from privyx.app._services._agents import Launch
    from privyx.cli.commands.run import _show_outcome, _show_running

    app = Privyx()
    settings = run_settings(app, spec.provider, upstream, session_strategy, no_anchor).settings
    plan = plan_launch(spec, argv, settings, 0, spec.env_vars)
    launch = Launch(spec.command, spec, argv, spec.env_vars, settings, {}, plan)
    outcome = await app.agents.serve(launch, announce=_show_running)
    _show_outcome(outcome, False, command=spec.command)
    return outcome.code
