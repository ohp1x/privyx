"""Upstream cookies: none kept between requests, and each reaches the client on its own."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.providers import GenericProvider
from privyx.proxy import Pipeline
from privyx.proxy._asgi import create_app
from privyx.vaults import MemoryVault

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


async def _post(
    pipeline: Pipeline, path: str = "/v1/chat/completions", **headers: str
) -> httpx.Response:
    """One request from a client of its own, whose cookie jar starts empty."""
    transport = httpx.ASGITransport(app=create_app(pipeline))
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as client:
        return await client.post(path, content=CHAT, headers={**JSON_HEADERS, **headers})


@pytest.mark.parametrize("gateway", [False, True], ids=["transparent", "gateway"])
async def test_a_cookie_of_one_reply_does_not_go_out_on_the_next_request(
    gateway: bool, upstream: _Upstream
) -> None:
    pipeline = (
        Pipeline(_engine(), GenericProvider(base_url=upstream.url + "/v1"), gateway=True)
        if gateway
        else Pipeline(_engine(), origin=upstream.url)
    )

    await _post(pipeline)
    await _post(pipeline)
    await pipeline.close()

    assert upstream.cookies == [None, None]


@pytest.mark.parametrize("path", list(REPLIES), ids=["batch", "stream", "relay"])
async def test_each_set_cookie_reaches_the_client_on_its_own(
    path: str, upstream: _Upstream
) -> None:
    pipeline = Pipeline(_engine(), origin=upstream.url)

    reply = await _post(pipeline, path)
    await pipeline.close()

    assert reply.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]


@pytest.mark.parametrize(("forward", "sent"), [(True, "login=c")])
async def test_the_clients_own_cookie_follows_forward_client_auth(
    forward: bool, sent: str | None, upstream: _Upstream
) -> None:
    pipeline = Pipeline(_engine(), origin=upstream.url, forward_client_auth=forward)

    await _post(pipeline, cookie="login=c")
    await pipeline.close()

    assert upstream.cookies == [sent]
