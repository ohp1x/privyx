"""``privyx run``: the proxy an agent is launched against, and what its traffic showed.

The agent's requests reach the app ``privyx proxy`` serves, composed the same
way, with two edge middlewares of the launch's own: :class:`RequestCounter`,
which counts every HTTP request on every route, and the profile's budget.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.cli.budget import Budget
    from privyx.cli.launch import LaunchPlan
    from privyx.config.schema import BudgetConfig, Settings

#: Edge priorities: the counter outermost, so a request the budget refuses counts.
_COUNTER = 1000
_BUDGET = 900


class RequestCounter:
    """Edge middleware counting the HTTP requests that reach Privyx, on every route.

    Requests that never become an exchange count too, so a tool that ignores
    the proxy URL can be told from one whose requests all failed.
    """

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, app: Any) -> Any:
        async def counted(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                self.requests += 1
            await app(scope, receive, send)

        return counted


class _BudgetGuard:
    """The budget middleware of one launch, kept for its totals after the run."""

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.budget: Budget | None = None

    def __call__(self, app: Any) -> Budget:
        from privyx.cli.budget import Budget

        self.budget = Budget(app, **self.options)
        return self.budget


@dataclass(frozen=True)
class RunOutcome:
    """What a launch showed: the agent's exit code, and the traffic Privyx saw."""

    code: int
    #: HTTP requests that reached Privyx, on any route.
    requests: int
    #: When none went upstream on a masking route: how many passed through
    #: unmasked, were blocked, or failed.  ``None`` when one did.
    unproxied: dict[str, int] | None
    #: Whether a request sent a body Privyx relayed unmasked.
    unmasked: bool
    #: Why the budget stopped the agent, after how many turns and tokens.
    stopped: tuple[str, int, int] | None


async def run_agent(
    app: Privyx,
    settings: Settings,
    plan: LaunchPlan,
    sock: socket.socket,
    port: int,
    *,
    budget: BudgetConfig,
    launch: Callable[[LaunchPlan], Awaitable[int]],
    quiet_signals: bool = False,
) -> RunOutcome:
    """Serve ``app`` on ``sock`` for the agent of ``plan``, and report on the run.

    ``launch`` runs the agent once the proxy accepts connections, given the plan
    with its port, and returns its exit code.  With ``quiet_signals``, as in
    the worker of an agent started by exec, uvicorn leaves SIGTERM to the caller.

    Raises:
        ConfigError: If the app cannot be built.
    """
    import uvicorn

    from privyx.app._build import build_audit_logger
    from privyx.app._serve import compose, running
    from privyx.observability.audit import AuditEventType

    profile = Path(plan.profile).name  # the base name, so the trail holds no user paths
    try:
        audit = build_audit_logger(settings)
    except BaseException:
        sock.close()
        raise
    try:
        counter = RequestCounter()
        app.add_middleware(counter, priority=_COUNTER)
        guard = None
        if budget.max_turns or budget.max_tokens:
            guard = _BudgetGuard(
                budget=budget, routes=settings.proxy.routes, audit=audit, profile=profile
            )
            app.add_middleware(guard, priority=_BUDGET)
        asgi, router = compose(app, settings, lifespan=False)
        async with running(app, settings, router, audit=audit, run_token=plan.run_token):
            server: Any = uvicorn.Server(
                uvicorn.Config(
                    asgi,
                    host=settings.host,
                    port=port,
                    log_level="warning",  # the tool owns the terminal; stay quiet
                    ws="none",  # a WebSocket upgrade arrives as a plain HTTP request
                    log_config=None,  # uvicorn's records go to Privyx's handlers, not stderr
                )
            )
            if quiet_signals:
                server.capture_signals = nullcontext
            serve_task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                await _wait_until_started(server, serve_task)
                code = await launch(plan.bind_port(port))
                # Drain requests still in flight first: one being masked when the
                # tool exited may yet go out on a masking route.
                server.should_exit = True
                await serve_task
                unproxied: dict[str, Any] | None = None
                if not audit.routed:
                    # What a tool that ignores the proxy URL looks like from here, or
                    # one whose model requests take a path outside proxy.routes.
                    stats = audit.stats
                    unproxied = {
                        "passthrough": stats.events[AuditEventType.PROXY_PASSTHROUGH],
                        "blocked": stats.events[AuditEventType.PROXY_BLOCKED],
                        "failed": sum(stats.errors.values()),
                    }
                    audit.emit(
                        AuditEventType.RUN_UNPROXIED,
                        profile=profile,
                        requests=counter.requests,
                        **unproxied,
                    )
                stopped = guard.budget if guard is not None else None
                return RunOutcome(
                    code=code,
                    requests=counter.requests,
                    unproxied=unproxied,
                    unmasked=bool(audit.unmasked_bodies),
                    stopped=(stopped.exceeded, stopped.turns, stopped.tokens)
                    if stopped is not None and stopped.exceeded
                    else None,
                )
            finally:
                server.should_exit = True
                await serve_task
    finally:
        sock.close()
        audit.close()


async def _wait_until_started(server: Any, serve_task: asyncio.Task[Any]) -> None:
    """Block until uvicorn reports it is accepting connections.

    Raises:
        RuntimeError: If the server task exits before becoming ready — e.g. the
            port is taken.  Without this the tool would launch against a dead
            proxy and fail with a confusing connection error.
    """
    while not server.started:
        if serve_task.done():
            await serve_task  # re-raise the startup failure
            raise RuntimeError("proxy exited before it finished starting")
        await asyncio.sleep(0.02)
