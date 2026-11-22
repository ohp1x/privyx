"""The ``budget`` feature: stop an agent ``privyx run`` launched once it loops.

A *turn* is one model request; token counting and embeddings are not
(:meth:`~privyx.dialects.Dialect.turn`).  Tokens are what the upstream reports
in each turn's ``usage``, input and output, cached ones included.  A limit is
checked before each turn, so the turn that crosses it completes and the next
one is refused.

A format whose streamed reply carries ``usage`` only when asked (OpenAI's Chat
Completions) is asked, with a token limit.  A reply that still comes without
usage leaves the token budget unmeasured, and the next turn is refused rather
than counted as free.  A stream that breaks counts what it reported; without
usage it is not unmeasured, so one network failure does not end the launch.

Turns are not queued: parallel subagents keep running at once, so a limit can
be passed by the turns in flight when it is reached.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from privyx.core import EventBus, Services, listen
from privyx.events import (
    ProxyError,
    ProxyResponse,
    RequestEnded,
    RequestReceived,
    RunBudgetExceeded,
)
from privyx.features._base import Feature


class Budget(Feature):
    """Refuses a launch's model turns once its turn or token limit is reached."""

    name = "budget"
    single_process = True

    class Options(Feature.Options):
        max_turns: int | None = Field(default=None, gt=0)
        max_tokens: int | None = Field(default=None, gt=0)
        #: The agent profile launched, named in the audit trail and the refusal.
        profile: str = ""

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.turns = 0
        self.tokens = 0
        #: A turn's reply came without usage.
        self.unmeasured = False
        #: Why the launch was stopped, once it is.
        self.exceeded: str | None = None
        self._pending: set[str] = set()  # the turns whose replies are still to come
        self._events: EventBus | None = None

    async def open(self, services: Services) -> None:
        self._events = services.events

    @listen(RequestReceived, priority=100)
    def check(self, event: RequestReceived) -> None:
        if not event.dialect.turn(event.path):
            return  # count_tokens and the like are free
        reached = self._reached()
        if reached is not None:
            kind, reason = reached
            self._stop(kind, reason)
            # 400, not 429: an agent retries a 429, and the next one is refused too.
            event.reject(
                400,
                "privyx_budget_exceeded",
                f"privyx stopped this session: the {reason} (agent profile "
                f"{self.options.profile!r}); start a new `privyx run` to continue",
            )
        self.turns += 1
        self._pending.add(event.request_id)
        if self.options.max_tokens is not None:
            event.dialect.ask_usage(event.body)

    @listen(ProxyResponse)
    def count(self, event: ProxyResponse) -> None:
        if event.request_id not in self._pending:
            return
        self._pending.discard(event.request_id)
        if event.usage:
            self.tokens += sum(event.usage.values())
        elif 200 <= event.status < 300 and not event.aborted:
            self.unmeasured = True  # the next turn is refused

    @listen(ProxyError)
    def count_failed(self, event: ProxyError) -> None:
        if event.request_id in self._pending:
            self._pending.discard(event.request_id)
            self.tokens += sum((event.usage or {}).values())

    @listen(RequestEnded)
    def forget(self, event: RequestEnded) -> None:
        self._pending.discard(event.request_id)

    def _reached(self) -> tuple[str, str] | None:
        """The limit reached, as its audit name and a sentence, or ``None``."""
        if self.unmeasured and self.options.max_tokens is not None:
            return "unmeasured", "token budget cannot be measured: a reply came without usage"
        for kind, used, limit in (
            ("turn", self.turns, self.options.max_turns),
            ("token", self.tokens, self.options.max_tokens),
        ):
            if limit is not None and used >= limit:
                return kind, f"{kind} budget ({limit} {kind}{'s' * (limit != 1)}) is reached"
        return None

    def _stop(self, kind: str, reason: str) -> None:
        if self.exceeded is not None:
            return
        self.exceeded = reason
        if self._events is not None:
            fields = {
                "profile": self.options.profile,
                "limit": kind,
                "turns": self.turns,
                "tokens": self.tokens,
            }
            self._events.notify(RunBudgetExceeded(timestamp=time.time(), fields=fields))
