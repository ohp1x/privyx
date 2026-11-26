"""The agents service: the profiles ``privyx run`` knows, the plan of a launch, and the launch."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core import ConfigError, PrivyxError

if TYPE_CHECKING:
    import socket

    from privyx.agents import LaunchPlan
    from privyx.agents._plan import Target
    from privyx.app._privyx import Privyx
    from privyx.app._run import RunOutcome
    from privyx.config.schema import Settings


@dataclass(frozen=True)
class AgentInfo:
    """An agent profile, as ``privyx run --list`` shows it."""

    name: str
    type: str
    provider: str
    env_vars: list[str]


@dataclass(frozen=True)
class Launch:
    """A planned launch: the plan, and what it was resolved from."""

    #: The profile or command, after a target of a type's own syntax is expanded.
    name: str
    spec: Target
    argv: list[str]
    #: The variables set to the proxy URL: the profile's, then the caller's.
    env_vars: tuple[str, ...]
    settings: Settings
    sources: dict[str, str]
    plan: LaunchPlan


class Agents:
    class BadTarget(ConfigError):
        """The target names nothing that can be launched."""

    class NotInstalled(PrivyxError):
        """The agent's command is not on ``PATH``."""

    def __init__(self, app: Privyx) -> None:
        self._app = app

    def list(self, target: str | None = None, **options: Any) -> list[AgentInfo]:
        """The agent profiles by name, as the settings for ``target`` define them.

        ``options`` are those of :meth:`plan`.

        Raises:
            ConfigError: If the settings cannot be read.
        """
        from privyx.app._run import run_settings

        settings = run_settings(self._app, agent=target, **options).settings
        return [
            AgentInfo(name, profile.type, profile.provider, profile.env_vars)
            for name, profile in sorted(settings.agents.items())
        ]

    def plan(
        self,
        target: str,
        args: Sequence[str] = (),
        *,
        port: int = 0,
        port_given: bool = False,
        env_vars: tuple[str, ...] = (),
        **options: Any,
    ) -> Launch:
        """The plan of launching ``target`` with ``args``; nothing is started.

        Args:
            target: A profile, a target an agent type spells its own way, or a command.
            args: The arguments for the agent.
            port: The proxy's port; 0 picks a free one.
            port_given: Whether the user chose ``port``, for where the plan says it came from.
            env_vars: Further variables to set to the proxy URL.
            **options: ``provider``, ``upstream``, ``session_strategy``,
                ``no_anchor``, and ``harvest``, as the flags of ``privyx run``.

        Raises:
            BadTarget: If ``target`` is malformed, or nothing tells how to
                point it at the proxy.
            PrivyxError: If the settings cannot be read or the launch resolved.
        """
        from privyx.agents import Agent
        from privyx.app._run import expand, launch_target, plan_launch, run_settings

        loaded = run_settings(self._app, agent=target, **options)
        try:
            expanded = (
                expand(self._app, target, tuple(args))
                if target not in loaded.settings.agents
                else None
            )
        except ConfigError as exc:
            raise self.BadTarget(str(exc)) from exc
        if expanded is not None:
            target, args = expanded
            loaded = run_settings(self._app, agent=target, **options)
        settings, sources = loaded.settings, loaded.sources
        spec = launch_target(self._app, settings, target, env_vars=env_vars, sources=sources)
        all_env_vars = tuple(spec.env_vars) + tuple(env_vars)
        # No variable and no argument of its type's points the tool at the proxy.
        if not all_env_vars and type(spec.agent).argv is Agent.argv:
            raise self.BadTarget(_no_profile(target, settings))
        sources["run.port"] = "CLI" if port_given else "run defaults"
        argv = [*args]
        plan = plan_launch(
            spec,
            argv,
            settings,
            port,
            all_env_vars,
            name=target,
            extra_env_vars=env_vars,
            sources=sources,
        )
        return Launch(target, spec, argv, all_env_vars, settings, sources, plan)

    def harvest(self, launch: Launch) -> str | None:
        """Add what the working directory tells to the launch's masking, if it is switched on.

        Returns:
            What it masks, as counts, or ``None`` when harvesting is off.
        """
        if not launch.settings.harvest:
            return None
        from privyx.workspace import harvest

        return harvest(launch.settings, Path.cwd())

    def check(self, launch: Launch) -> None:
        """Check that the agent can be started, and what its type needs is there.

        Raises:
            NotInstalled: If its command is not on the ``PATH`` it would run with.
            PrivyxError: If its type's own check fails.
        """
        plan = launch.plan
        if shutil.which(plan.command, path=plan.environment.get("PATH")) is None:
            raise self.NotInstalled(f"{launch.spec.command!r} not found on PATH")
        launch.spec.agent.check(plan.upstream)

    def run(
        self,
        launch: Launch,
        *,
        announce: Callable[[LaunchPlan], None] | None = None,
        report: Callable[[RunOutcome, bool], None] | None = None,
    ) -> int:
        """Launch the agent and return its exit code; with exec, this never returns.

        In a terminal's foreground the agent replaces this process and the
        proxy runs in a worker.  Otherwise both run from here.

        Args:
            launch: What :meth:`plan` returned.
            announce: Called with the plan, its port bound, just before the agent starts.
            report: Called with what the launch showed, in the process that
                served the proxy, once the agent has exited, and whether the
                agent replaced this process, after which the shell has likely
                drawn its prompt.

        Raises:
            PrivyxError: If the proxy cannot start or the agent cannot be launched.
        """
        from privyx.app._run import can_exec_agent, exec_agent

        shown = announce or (lambda _: None)

        async def start(ready: socket.socket | None = None, parent: int | None = None) -> int:
            outcome = await self.serve(launch, announce=shown, ready=ready, parent=parent)
            if report is not None:
                report(outcome, parent is not None)
            return outcome.code

        if can_exec_agent():
            exec_agent(launch.plan, start, shown)
        return asyncio.run(start())

    async def serve(
        self,
        launch: Launch,
        *,
        announce: Callable[[LaunchPlan], None],
        ready: socket.socket | None = None,
        parent: int | None = None,
    ) -> RunOutcome:
        """Start the proxy, run the tool against it, and tear everything down.

        ``proxy.mode`` picks the app, as in ``privyx proxy``.  With ``ready``
        and ``parent``, the agent is the process ``parent``, already started
        by exec; otherwise it runs as a child, after ``announce``.

        Returns:
            The tool's exit code and what its traffic showed.
        """
        from privyx.app._run import listen, run_agent, spawn, wait_for_agent_exit
        from privyx.observability.logging import configure_logging

        app, settings, plan = self._app, launch.settings, launch.plan
        # The child tool owns the terminal: log lines go to log.file if one is set,
        # else nowhere, and the audit trail still records every error.
        configure_logging(settings, console=False)
        # Bound before the rest opens, so a port in use leaves nothing to undo.
        sock, bound_port = listen(settings.server.host, plan.port)

        async def start_agent(bound: LaunchPlan) -> int:
            # On stderr: stdout is the tool's, and may be piped into a parser.
            if ready is not None and parent is not None:
                ready.sendall(json.dumps({"port": bound.port}).encode() + b"\n")
                ready.close()
                await wait_for_agent_exit(parent)
                return 0
            announce(bound)
            return await spawn(bound)

        return await run_agent(
            app,
            settings,
            plan,
            sock,
            bound_port,
            budget=launch.spec.budget,
            launch=start_agent,
            # The worker handles SIGTERM itself; uvicorn must not re-raise it during cleanup.
            quiet_signals=parent is not None,
        )


def _no_profile(target: str, settings: Settings) -> str:
    """Why ``target`` cannot be launched, with the profile it may have meant."""
    import difflib
    import re

    base = re.split(r"[:-]", target, maxsplit=1)[0]
    near = (
        [base]
        if base != target and base in settings.agents
        else difflib.get_close_matches(target, [*settings.agents], n=1)
    )
    hint = f" (did you mean {near[0]!r}?)" if near else ""
    return (
        f"no agent profile {target!r}{hint}; define it under `agents` "
        "or, to launch a tool directly, pass --provider openai or anthropic "
        "(sets the usual base-URL variables) or --env-var NAME "
        "(see `privyx run --list`)"
    )
