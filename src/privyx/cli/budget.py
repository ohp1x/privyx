"""Per-launch budget for ``privyx run``: stop an agent that loops.

A *turn* is one model request on a masked chat route; token counting routes
(``count_tokens``, ``input_tokens``) are free.  Tokens are what the upstream
reports in each reply's ``usage``, input and output, cached ones included.
A limit is checked before each turn, so the turn that crosses it completes and
the next one is refused.

A streamed Chat Completions reply carries ``usage`` only when the request asks
for it, so with a token limit such a request is sent with
``stream_options.include_usage``.  A reply that still comes without usage
leaves the token budget unmeasured, and the next turn is refused rather than
counted as free.
"""

from __future__ import annotations

import json
from typing import Any

from privyx.config.schema import BudgetConfig
from privyx.observability.audit import AuditEventType, AuditLogger
from privyx.observability.usage import merge_usage, usage_tokens
from privyx.proxy.schemas import detect_schema

#: Schemas whose requests are model turns.
_CHAT = frozenset({"openai", "anthropic", "responses", "gemini"})
#: Path endings of routes that are not model turns: token counting, and
#: Gemini's embeddings and cached contents, which share the chat schema.
_FREE = (
    "/count_tokens",
    "/input_tokens",
    ":countTokens",
    ":embedContent",
    ":batchEmbedContents",
    "/cachedContents",
)


class Budget:
    """An ASGI app that refuses model requests to ``app`` once a limit is reached."""

    def __init__(
        self,
        app: Any,
        budget: BudgetConfig,
        routes: dict[str, str],
        audit: AuditLogger,
        profile: str,
    ) -> None:
        self.app = app
        self.budget = budget
        self.routes = routes
        self.audit = audit
        self.profile = profile
        self.turns = 0
        self.tokens = 0
        self.unmeasured = False  # a reply came without usage
        self.exceeded: str | None = None  # why the session was stopped

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if detect_schema(path, self.routes) not in _CHAT or path.endswith(_FREE):
            await self.app(scope, receive, send)
            return
        reached = self._reached()
        if reached is not None:
            await self._refuse(*reached, send)
            return
        self.turns += 1
        if self.budget.max_tokens is not None and detect_schema(path, self.routes) == "openai":
            scope, receive = await _ask_usage(scope, receive)
        await self.app(scope, receive, self._metered(send))

    def _reached(self) -> tuple[str, str] | None:
        """The limit reached, as its audit name and a sentence, or ``None``."""
        if self.unmeasured and self.budget.max_tokens is not None:
            return "unmeasured", "token budget cannot be measured: a reply came without usage"
        for kind, used, limit in (
            ("turn", self.turns, self.budget.max_turns),
            ("token", self.tokens, self.budget.max_tokens),
        ):
            if limit is not None and used >= limit:
                return kind, f"{kind} budget ({limit} {kind}{'s' * (limit != 1)}) is reached"
        return None

    async def _refuse(self, kind: str, reason: str, send: Any) -> None:
        if self.exceeded is None:
            self.exceeded = reason
            self.audit.emit(
                AuditEventType.RUN_BUDGET_EXCEEDED,
                profile=self.profile,
                limit=kind,
                turns=self.turns,
                tokens=self.tokens,
            )
        body = json.dumps(
            {
                "error": {
                    "type": "privyx_budget_exceeded",
                    "message": f"privyx stopped this session: the {reason} "
                    f"(agent profile {self.profile!r}); start a new `privyx run` to continue",
                }
            }
        ).encode()
        # 400, not 429: an agent retries a 429, and the next one is refused too.
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _metered(self, send: Any) -> Any:
        fields: dict[str, int] = {}
        stream = False
        status = 0
        pending = b""  # an SSE line, or a JSON body, not yet whole

        async def metered(message: dict[str, Any]) -> None:
            nonlocal stream, status, pending
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                stream = b"text/event-stream" in headers.get(b"content-type", b"")
                status = message.get("status", 0)
            elif message["type"] == "http.response.body":
                pending += message.get("body", b"")
                if stream:
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        if line.startswith(b"data:"):
                            merge_usage(fields, _loads(line[5:]))
                if not message.get("more_body", False):
                    if pending.startswith(b"data:"):
                        pending = pending[5:]
                    body = _loads(pending)
                    # A Gemini stream asked for without SSE is one JSON array.
                    for item in body if isinstance(body, list) else [body]:
                        merge_usage(fields, item)
                    self.tokens += usage_tokens(fields)
                    # A reply that ends, not one the client cancelled midway.
                    if 200 <= status < 300 and not fields:
                        self.unmeasured = True
            await send(message)

        return metered


async def _ask_usage(scope: Any, receive: Any) -> tuple[Any, Any]:
    """Ask a streamed Chat Completions request for ``usage``; return its scope and body.

    A request that says ``include_usage`` itself is left as it is.
    """
    messages = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request" or not message.get("more_body", False):
            break
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
    payload = _loads(body)
    options = payload.get("stream_options") if isinstance(payload, dict) else None
    if (
        isinstance(payload, dict)
        and payload.get("stream") is True
        and (options is None or (isinstance(options, dict) and "include_usage" not in options))
    ):
        payload["stream_options"] = {**(options or {}), "include_usage": True}
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"content-length"]
        scope = {**scope, "headers": [*headers, (b"content-length", str(len(body)).encode())]}
        messages = [{"type": "http.request", "body": body, "more_body": False}]
    pending = list(messages)

    async def replay() -> Any:
        return pending.pop(0) if pending else await receive()

    return scope, replay


def _loads(data: bytes) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None
