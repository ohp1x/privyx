"""Upstream cookies: none kept between requests, and each reaches the client on its own."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from privyx.config.schema import Settings  # noqa: E402
from privyx.core.engine import PrivacyEngine  # noqa: E402
from privyx.gateway.server import Gateway  # noqa: E402
from privyx.gateway.transparent import create_transparent_app  # noqa: E402
from privyx.privacy.detector.builtin import RegexDetector  # noqa: E402
from privyx.privacy.operator.pseudonym import PseudonymOperator  # noqa: E402
from privyx.privacy.policy.default import DefaultPolicy  # noqa: E402
from privyx.providers.generic import GenericProvider  # noqa: E402
from privyx.proxy.transparent import TransparentProxy  # noqa: E402
from privyx.vault.memory import MemoryVault  # noqa: E402

CHAT = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
JSON_HEADERS = {"content-type": "application/json"}
REPLIES = {
    "/v1/chat/completions": ("application/json", b'{"choices":[]}'),
    "/v1/messages": ("text/event-stream", b'data: {"type":"ping"}\n\n'),
    "/v1/files": ("text/plain", b"raw"),
}


class _Upstream:
    """A real server, so that the real client and its cookie jar run."""

    def __init__(self) -> None:
        self.cookies: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.cookies.append(self.headers.get("cookie"))
                content_type, body = REPLIES.get(self.path, REPLIES["/v1/chat/completions"])
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("set-cookie", "a=1; Path=/")
                self.send_header("set-cookie", "b=2; Path=/")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Upstream]:
    monkeypatch.setenv("NO_PROXY", "*")
    server = _Upstream()
    yield server
    server.server.shutdown()
    server.server.server_close()


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


async def _post(app: Any, path: str = "/v1/chat/completions", **headers: str) -> httpx.Response:
    """One request from a client of its own, whose cookie jar starts empty."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as client:
        return await client.post(path, content=CHAT, headers={**JSON_HEADERS, **headers})


async def test_a_cookie_of_one_reply_does_not_go_out_on_the_next_request_transparent(
    upstream: _Upstream,
) -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin=upstream.url)
    app = create_transparent_app(engine, proxy)

    await _post(app)
    await _post(app)
    await proxy.close()

    assert upstream.cookies == [None, None]


async def test_a_cookie_of_one_reply_does_not_go_out_on_the_next_request_gateway(
    upstream: _Upstream,
) -> None:
    provider = GenericProvider(base_url=upstream.url + "/v1/chat/completions")
    gateway = Gateway(engine=_engine(), provider=provider, settings=Settings())

    await _post(gateway.app)
    await _post(gateway.app)
    await provider.close()

    assert upstream.cookies == [None, None]


@pytest.mark.parametrize("path", list(REPLIES), ids=["batch", "stream", "relay"])
async def test_each_set_cookie_reaches_the_client_on_its_own(
    path: str, upstream: _Upstream
) -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin=upstream.url)

    reply = await _post(create_transparent_app(engine, proxy), path)
    await proxy.close()

    assert reply.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]


@pytest.mark.parametrize(("forward", "sent"), [(True, "login=c")])
async def test_the_clients_own_cookie_follows_forward_client_auth(
    forward: bool, sent: str | None, upstream: _Upstream
) -> None:
    engine = _engine()
    proxy = TransparentProxy(engine, origin=upstream.url, forward_client_auth=forward)

    await _post(create_transparent_app(engine, proxy), cookie="login=c")
    await proxy.close()

    assert upstream.cookies == [sent]
