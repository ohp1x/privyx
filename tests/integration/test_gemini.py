"""Gemini's native API through the transparent proxy and the gateway.

The mock upstream speaks Gemini: it echoes the last user text back, cut into
chunks, as ``streamGenerateContent`` (SSE with ``alt=sse``, else one JSON
array) or ``generateContent``, and answers with a ``functionCall`` that repeats
the text as an argument.  A reply that comes back with the original proves the
request was masked upstream and the reply restored.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.providers.google import GoogleProvider
from privyx.providers.registry import build_provider
from privyx.proxy.transparent import TransparentProxy
from privyx.streaming.sse import SSEDecoder
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
KEY = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW"
MODEL_PATH = "v1beta/models/gemini-2.5-flash"


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def _upstream(capture: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        body = json.loads(request.content)
        text = body["contents"][-1]["parts"][0]["text"]
        pieces = [text[i : i + 5] for i in range(0, len(text), 5)]
        chunks: list[dict[str, Any]] = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": p}]}, "index": 0}]}
            for p in pieces
        ]
        call = {"functionCall": {"name": "note", "args": {"name": text}}}
        chunks.append(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [call]},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
            }
        )
        if request.url.path.endswith(":streamGenerateContent"):
            if request.url.params.get("alt") == "sse":
                frames = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=frames.encode()
                )
            return httpx.Response(200, json=chunks)
        whole = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}, call]},
                    "finishReason": "STOP",
                }
            ]
        }
        return httpx.Response(200, json=whole)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _request() -> bytes:
    content = {"role": "user", "parts": [{"text": f"mail {EMAIL}"}]}
    return json.dumps({"contents": [content]}).encode()


def _parts(chunks: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    parts = [p for c in chunks for p in c["candidates"][0]["content"]["parts"]]
    return "".join(p.get("text", "") for p in parts), [p for p in parts if "functionCall" in p]


async def _transparent(path: str, query: list[tuple[str, str]] | None = None) -> Any:
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(
        _engine(), origin="https://gemini.test", client=_upstream(capture), api_key=KEY
    )
    response = await proxy.handle(
        method="POST",
        path=path,
        headers={"content-type": "application/json", "x-goog-api-key": "client-key"},
        body=_request(),
        query_params=query,
    )
    body = response.body
    if body is None:
        assert response.stream is not None
        body = b"".join([c.encode() if isinstance(c, str) else c async for c in response.stream])
    sent = capture[0]
    assert EMAIL not in sent.content.decode()
    assert "<PRIVYX_EMAIL_1>" in sent.content.decode()
    assert sent.headers["x-goog-api-key"] == KEY
    assert "authorization" not in sent.headers
    return response, body.decode()


async def test_transparent_sse_stream_is_restored() -> None:
    response, body = await _transparent(f"{MODEL_PATH}:streamGenerateContent", [("alt", "sse")])

    assert response.media_type == "text/event-stream"
    decoder = SSEDecoder()
    chunks = [json.loads(e.data) for e in decoder.feed(body) + decoder.flush()]
    text, calls = _parts(chunks)
    assert text == f"mail {EMAIL}"
    assert calls == [{"functionCall": {"name": "note", "args": {"name": f"mail {EMAIL}"}}}]


async def test_transparent_json_array_stream_is_restored() -> None:
    _, body = await _transparent(f"{MODEL_PATH}:streamGenerateContent")

    text, calls = _parts(json.loads(body))
    assert text == f"mail {EMAIL}"
    assert calls[0]["functionCall"]["args"] == {"name": f"mail {EMAIL}"}


async def test_transparent_batch_reply_is_restored() -> None:
    _, body = await _transparent(f"{MODEL_PATH}:generateContent")

    parts = json.loads(body)["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": f"mail {EMAIL}"}
    assert parts[1]["functionCall"]["args"] == {"name": f"mail {EMAIL}"}


# -- gateway ----------------------------------------------------------------


def _gateway(capture: list[httpx.Request], base_url: str = "") -> httpx.AsyncClient:
    pytest.importorskip("fastapi")
    from privyx.gateway.server import Gateway

    settings = Settings(provider={"type": "google", "base_url": base_url, "api_key": KEY})
    provider = build_provider(settings)
    assert isinstance(provider, GoogleProvider)
    provider._client = _upstream(capture)  # the mock upstream
    gateway = Gateway(engine=_engine(), provider=provider, settings=settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://p")


@pytest.mark.parametrize(
    ("query", "stream"), [("?alt=sse", True), ("", False)], ids=["sse", "array"]
)
async def test_gateway_streams_to_the_model_the_client_named(query: str, stream: bool) -> None:
    capture: list[httpx.Request] = []
    async with _gateway(capture) as client:
        response = await client.post(
            f"/{MODEL_PATH}:streamGenerateContent{query}", content=_request()
        )

    assert response.status_code == 200
    sent = capture[0]
    assert str(sent.url) == (
        f"https://generativelanguage.googleapis.com/{MODEL_PATH}:streamGenerateContent{query}"
    )
    assert sent.headers["x-goog-api-key"] == KEY
    assert EMAIL not in sent.content.decode()
    if stream:
        decoder = SSEDecoder()
        chunks = [json.loads(e.data) for e in decoder.feed(response.text) + decoder.flush()]
    else:
        chunks = response.json()
    text, calls = _parts(chunks)
    assert text == f"mail {EMAIL}"
    assert calls[0]["functionCall"]["args"] == {"name": f"mail {EMAIL}"}


@pytest.mark.parametrize(
    ("base_url", "path", "url"),
    [
        # An origin: the client's own path, its version included.
        (
            "https://g.test",
            "v1alpha/tunedModels/t:generateContent",
            "https://g.test/v1alpha/tunedModels/t:generateContent",
        ),
        ("https://g.test", "v1beta/cachedContents", "https://g.test/v1beta/cachedContents"),
        # A root with a path: it takes the place of the client's version.
        (
            "https://g.test/v1",
            "v1beta/models/m:countTokens",
            "https://g.test/v1/models/m:countTokens",
        ),
    ],
)
async def test_gateway_posts_the_path_the_client_called(base_url: str, path: str, url: str) -> None:
    capture: list[httpx.Request] = []
    async with _gateway(capture, base_url=base_url) as client:
        response = await client.post(f"/{path}", content=_request())

    assert response.status_code == 200
    assert str(capture[0].url) == url


async def test_gateway_base_url_keeps_its_root() -> None:
    capture: list[httpx.Request] = []
    vertex = "https://us-central1-aiplatform.test/v1/projects/p/locations/l/publishers/google"
    async with _gateway(capture, base_url=f"{vertex}/models/fixed:generateContent") as client:
        response = await client.post(f"/{MODEL_PATH}:generateContent", content=_request())

    assert response.status_code == 200
    assert str(capture[0].url) == f"{vertex}/models/gemini-2.5-flash:generateContent"
    parts = response.json()["candidates"][0]["content"]["parts"]
    assert parts[0] == {"text": f"mail {EMAIL}"}


async def test_gateway_serves_gemini_routes_only_with_the_google_provider() -> None:
    pytest.importorskip("fastapi")
    from privyx.gateway.server import Gateway

    settings = Settings(provider={"type": "openai"})
    gateway = Gateway(engine=_engine(), provider=build_provider(settings), settings=settings)

    assert "gemini" not in gateway.routes.values()
    google = Settings(provider={"type": "google"})
    with_google = Gateway(engine=_engine(), provider=build_provider(google), settings=google)
    assert "/*/models/*:countTokens" in with_google.routes


@pytest.mark.parametrize("mode", ["transparent", "gateway"])
async def test_directive_reaches_generate_but_not_count_tokens(mode: str) -> None:
    pytest.importorskip("fastapi")
    from privyx.gateway.server import Gateway

    capture: list[httpx.Request] = []
    engine = _engine()
    engine.directive = True
    if mode == "gateway":
        settings = Settings(provider={"type": "google"})
        provider = build_provider(settings)
        provider._client = _upstream(capture)  # type: ignore[attr-defined]
        app = Gateway(engine=engine, provider=provider, settings=settings).app
    else:
        from privyx.gateway.transparent import create_transparent_app

        proxy = TransparentProxy(engine, origin="https://g.test", client=_upstream(capture))
        app = create_transparent_app(engine, proxy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://p"
    ) as client:
        for method in ("generateContent", "countTokens"):
            await client.post(f"/{MODEL_PATH}:{method}", content=_request())

    generate, count = (json.loads(r.content) for r in capture)
    assert "systemInstruction" in generate
    assert "systemInstruction" not in count
