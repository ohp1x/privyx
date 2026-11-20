"""``proxy.path_mapping``: an upstream URL with a path of its own."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from privyx.cli.commands.run import TARGETS  # noqa: E402
from privyx.cli.launch import build_launch_plan  # noqa: E402
from privyx.cli.main import cli  # noqa: E402
from privyx.config.schema import Settings  # noqa: E402
from privyx.core import ConfigError  # noqa: E402
from privyx.detectors import RegexDetector  # noqa: E402
from privyx.engine import PrivacyEngine  # noqa: E402
from privyx.gateway.transparent import create_transparent_app  # noqa: E402
from privyx.observability.audit import AuditLogger  # noqa: E402
from privyx.operators._pseudonym import PseudonymOperator  # noqa: E402
from privyx.policies import DefaultPolicy  # noqa: E402
from privyx.providers import resolve_transparent_upstream  # noqa: E402
from privyx.proxy.headers import RUN_TOKEN_HEADER, UPSTREAMS_HEADER  # noqa: E402
from privyx.proxy.transparent import TransparentProxy  # noqa: E402
from privyx.vaults import MemoryVault  # noqa: E402

EMAIL = "ana@example.com"
ROUTER = "https://router.example.com/api/v1"


def _proxy(
    seen: list[httpx.Request], origin: str = ROUTER, strip_prefix: str | None = "/v1", **kw
) -> TransparentProxy:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content) if request.content else {}
        content = body.get("messages", [{}])[-1].get("content", "")
        if body.get("stream"):
            frame = json.dumps({"choices": [{"delta": {"content": content}}]})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {frame}\n\ndata: [DONE]\n\n".encode(),
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    return TransparentProxy(
        engine,
        origin=origin,
        strip_prefix=strip_prefix,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kw,
    )


def _client(proxy: TransparentProxy) -> httpx.AsyncClient:
    app = create_transparent_app(proxy._engine, proxy)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://privyx")


def _chat(stream: bool = False) -> dict:
    return {"model": "m", "stream": stream, "messages": [{"role": "user", "content": EMAIL}]}


@pytest.mark.parametrize("stream", [False, True])
async def test_prefix_keeps_upstream_path_and_masks(stream: bool) -> None:
    seen: list[httpx.Request] = []
    async with _client(_proxy(seen)) as client:
        response = await client.post(
            "/v1/chat/completions?a=1&a=2", json=_chat(stream), headers={"x-test": "kept"}
        )
    assert response.status_code == 200
    assert EMAIL in response.text  # restored
    (request,) = seen
    assert str(request.url) == f"{ROUTER}/chat/completions?a=1&a=2"
    assert EMAIL.encode() not in request.content


@pytest.mark.parametrize(
    ("origin", "prefix", "path", "url"),
    [
        ("https://router.example.com/team/acme", "", "/v1/messages",
         "https://router.example.com/team/acme/v1/messages"),
        ("https://router.example.com", "", "/v1/messages", "https://router.example.com/v1/messages"),
        (ROUTER, "/v1", "/v1", ROUTER),
        (ROUTER, "/v1", "/v1/", ROUTER + "/"),
        (ROUTER, "/v1", "/v1/models", ROUTER + "/models"),
    ],
)  # fmt: skip
async def test_prefix_join(origin: str, prefix: str, path: str, url: str) -> None:
    seen: list[httpx.Request] = []
    proxy = _proxy(seen, origin=origin, strip_prefix=prefix)
    response = await proxy.handle(method="GET", path=path, headers={}, body=b"")
    assert response.status_code == 200
    assert str(seen[0].url) == url


@pytest.mark.parametrize(
    "path",
    [
        "/v10/chat/completions",  # not a segment boundary
        "/chat/completions",  # outside the prefix
        "/v1/../admin",
        "/v1/./chat/completions",
        "/v1/a\\b",
        "/v1/%2e%2e/admin",  # double-encoded: decoded once by the server
        "/v1/a?b",
        "/v1/a#b",
    ],
)
async def test_prefix_refuses_paths_outside_or_ambiguous(path: str) -> None:
    seen: list[httpx.Request] = []
    trail = io.StringIO()
    proxy = _proxy(seen, audit=AuditLogger(trail))
    response = await proxy.handle(method="POST", path=path, headers={}, body=b"{}")
    assert response.status_code == 404
    assert not seen
    event = json.loads(trail.getvalue().splitlines()[-1])
    assert event["event"] == "proxy.blocked" and event["reason"] == "outside_prefix"


async def test_prefix_refuses_encoded_separator() -> None:
    seen: list[httpx.Request] = []
    async with _client(_proxy(seen)) as client:
        response = await client.post("/v1/chat%2Fcompletions", json=_chat())
    assert response.status_code == 404
    assert not seen


TOKEN = "run-secret"
BASES = {
    "openai": "https://router.example.com/api/v1",
    "anthropic": "https://router.example.com/api",
    "gemini": "https://gateway.example.com/v1/acct/google/v1beta",
}


def _named(token: str = TOKEN, **bases: str) -> dict[str, str]:
    return {RUN_TOKEN_HEADER: token, UPSTREAMS_HEADER: json.dumps(bases or BASES)}


def _pi_proxy(seen: list[httpx.Request]) -> TransparentProxy:
    return _proxy(seen, origin="https://default.example.com", strip_prefix=None, run_token=TOKEN)


@pytest.mark.parametrize(
    ("path", "body", "url"),
    [
        ("/chat/completions", _chat(), f"{BASES['openai']}/chat/completions"),
        (
            "/v1/messages?beta=true",
            {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": EMAIL}]},
            f"{BASES['anthropic']}/v1/messages?beta=true",
        ),
        # Without its API version, which ends the base URL.
        (
            "/models/g:streamGenerateContent?alt=sse",
            {"contents": [{"role": "user", "parts": [{"text": EMAIL}]}]},
            f"{BASES['gemini']}/models/g:streamGenerateContent?alt=sse",
        ),
    ],
)
async def test_run_token_sends_each_route_to_the_base_url_it_names(
    path: str, body: dict, url: str
) -> None:
    seen: list[httpx.Request] = []
    async with _client(_pi_proxy(seen)) as client:
        response = await client.post(path, json=body, headers={**_named(), "x-api-key": "k"})
    assert response.status_code == 200
    (request,) = seen
    assert str(request.url) == url
    assert EMAIL.encode() not in request.content
    assert request.headers["x-api-key"] == "k"  # the client's own key, as sent
    assert RUN_TOKEN_HEADER not in request.headers
    assert UPSTREAMS_HEADER not in request.headers


async def test_pi_routing_needs_no_fixed_upstream_with_prefix_mapping() -> None:
    from privyx.cli.commands.proxy import build_transparent_proxy

    settings = Settings(proxy={"path_mapping": {"mode": "prefix"}})
    pi = TARGETS["pi"]
    plan = build_launch_plan(pi, [], settings, 0, pi.env_vars)
    engine = _proxy([])._engine
    with pytest.raises(ConfigError, match="prefix needs an upstream"):
        build_transparent_proxy(settings, engine, None)
    proxy = build_transparent_proxy(settings, engine, None, plan.run_token)
    await proxy.close()


async def test_gemini_path_without_its_version_is_masked() -> None:
    """pi's path when its base URL, ending in the version, is the proxy's."""
    seen: list[httpx.Request] = []
    gemini = "https://gemini.example.com/v1beta"
    body = {"contents": [{"role": "user", "parts": [{"text": EMAIL}]}]}
    async with _client(_proxy(seen, origin=gemini, strip_prefix="")) as client:
        response = await client.post("/models/g:generateContent", json=body)
    assert response.status_code == 200
    (request,) = seen
    assert str(request.url) == f"{gemini}/models/g:generateContent"
    assert EMAIL.encode() not in request.content


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _named(token="wrong"),
        {RUN_TOKEN_HEADER: TOKEN, UPSTREAMS_HEADER: "not json"},
        _named(anthropic=BASES["anthropic"]),  # none for this route
        _named(openai="https://user:pw@router.example.com/api/v1"),
        _named(openai="file:///api/v1"),
    ],
)
async def test_run_token_refuses_a_request_without_a_usable_upstream(
    headers: dict[str, str],
) -> None:
    seen: list[httpx.Request] = []
    async with _client(_pi_proxy(seen)) as client:
        response = await client.post("/chat/completions", json=_chat(), headers=headers)
        unrouted = await client.get("/models", headers=_named())
    assert response.status_code == unrouted.status_code == 403
    assert not seen


async def test_origin_mode_is_unchanged() -> None:
    seen: list[httpx.Request] = []
    proxy = _proxy(seen, origin="https://api.x.ai", strip_prefix=None)
    await proxy.handle(method="GET", path="v10/models", headers={}, body=b"")
    assert str(seen[0].url) == "https://api.x.ai/v10/models"


def test_config_validation() -> None:
    assert Settings().proxy.path_mapping.mode == "origin"
    mapping = Settings(proxy={"path_mapping": {"mode": "prefix", "strip_prefix": "/v1/"}})
    assert mapping.proxy.path_mapping.strip_prefix == "/v1"
    for bad in (
        {"mode": "gateway", "path_mapping": {"mode": "prefix"}},
        {"path_mapping": {"strip_prefix": "/v1"}},  # needs mode: prefix
        {"path_mapping": {"mode": "prefix", "strip_prefix": "v1"}},
        {"path_mapping": {"mode": "prefix", "strip_prefix": "/v1/../x"}},
        {"path_mapping": {"mode": "prefix", "strip_prefix": "/v1?x=1"}},
        {"path_mapping": {"mode": "path"}},
    ):
        with pytest.raises(ValidationError):
            Settings(proxy=bad)


def test_prefix_needs_explicit_upstream_without_query_or_credentials() -> None:
    prefix = {"path_mapping": {"mode": "prefix"}}
    with pytest.raises(ConfigError, match="needs an upstream URL"):
        resolve_transparent_upstream(Settings(proxy=prefix, provider={"type": "openai"}))
    for url in ("https://router.example.com/api?x=1", "https://ana:pw@router.example.com/api"):
        with pytest.raises(ConfigError, match="without query") as error:
            resolve_transparent_upstream(Settings(proxy=prefix, upstream_url=url))
        assert "pw" not in str(error.value)
    settings = Settings(proxy=prefix, provider={"base_url": ROUTER + "/"})
    assert resolve_transparent_upstream(settings) == ROUTER
    assert (
        resolve_transparent_upstream(Settings(upstream_url=ROUTER)) == "https://router.example.com"
    )


def test_run_strips_base_path_and_follows_endpoint_with_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("privyx.cli.claude._managed_root", lambda: tmp_path / "managed")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.example.com/api/anthropic")
    claude = TARGETS["claude"]
    with pytest.raises(ConfigError, match="path_mapping"):
        build_launch_plan(claude, [], Settings(), 0, claude.env_vars)
    settings = Settings(proxy={"path_mapping": {"mode": "prefix"}})
    plan = build_launch_plan(claude, [], settings, 0, claude.env_vars)
    assert plan.route == "https://router.example.com/api/anthropic"
    assert settings.proxy.path_mapping.strip_prefix == ""

    codex = TARGETS["codex"]
    settings = Settings(proxy={"path_mapping": {"mode": "prefix"}}, upstream_url=ROUTER)
    build_launch_plan(codex, [], settings, 0, codex.env_vars)
    assert settings.proxy.path_mapping.strip_prefix == "/v1"
    conflict = {"path_mapping": {"mode": "prefix", "strip_prefix": "/api"}}
    with pytest.raises(ConfigError, match="base_path"):
        build_launch_plan(codex, [], Settings(proxy=conflict, upstream_url=ROUTER), 0, ())


def test_run_dry_run_routes_grok_with_and_without_prefix(tmp_path: Path) -> None:
    def route(*config: str) -> str:
        result = CliRunner().invoke(cli, ["run", *config, "--dry-run", "grok"])
        assert result.exit_code == 0, result.output
        return next(line for line in result.output.splitlines() if line.startswith("Routing:"))

    assert route().endswith("/v1 → https://api.x.ai")
    config = tmp_path / "prefix.yaml"
    config.write_text("proxy: {path_mapping: {mode: prefix}}")
    assert route("-c", str(config)).endswith("/v1 → https://api.x.ai/v1")
