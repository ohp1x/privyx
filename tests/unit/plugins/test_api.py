"""The plugin API: the public module, ``Config``, ``Privyx``, and ``plugins.modules``."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from privyx.app import Privyx, include_router
from privyx.config.loader import load_config
from privyx.config.schema import Settings, json_schema
from privyx.core.builder import build_engine, build_operator, build_vault
from privyx.core.errors import ConfigError
from privyx.gateway.server import Gateway
from privyx.gateway.transparent import create_transparent_app
from privyx.plugins.loader import load_plugins
from privyx.plugins.registry import PLUGINS
from privyx.privacy.detector.yaml import build_detector
from privyx.providers.generic import GenericProvider
from privyx.token.codec import FormatCodec

# A detector with options checked by its Config, and an operator that reports
# the BuildContext it was given.
CONFIGURED = """
from typing import Literal

from privyx.plugins import BaseDetector, BaseOperator, BuildContext, ComponentConfig, Detection


class AcmeIdDetector(BaseDetector):
    name = "acme_id"

    class Config(ComponentConfig):
        region: Literal["EU", "US"] = "EU"
        prefix: str = "ACME-"

    def __init__(self, config):
        super().__init__()
        self.config = config

    @classmethod
    def from_config(cls, config, ctx):
        return cls(config)

    def detect_sync(self, text, context):
        found = Detection()
        at = text.find(self.config.prefix)
        if at != -1:
            found.add(at, at + len(self.config.prefix) + 4, "ACME_ID", text[at : at + 9])
        return found


class SeenOperator(BaseOperator):
    name = "seen"

    class Config(ComponentConfig):
        pass

    def __init__(self, ctx):
        self.ctx = ctx

    @classmethod
    def from_config(cls, config, ctx):
        return cls(ctx)

    async def pseudonymize(self, text, detection, session, context):
        raise NotImplementedError

    async def deanonymize(self, text, session, context):
        raise NotImplementedError
"""

STRICT_VAULT = """
from privyx.plugins import BaseVault, ComponentConfig


class ShelfVault(BaseVault):
    name = "shelf"

    class Config(ComponentConfig):
        shelf: str

    def __init__(self, config):
        self.config = config

    @classmethod
    def from_config(cls, config, ctx):
        return cls(config)

    async def create(self, session): ...
    async def get(self, session_id): ...
    async def save(self, session): ...
    async def delete(self, session_id): ...
"""


def _write(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(source, encoding="utf-8")
    return directory


def _config(tmp_path: Path, data: dict[str, Any]) -> Path:
    file = tmp_path / "privyx.yaml"
    file.write_text(json.dumps(data), encoding="utf-8")
    return file


# --- the public module ----------------------------------------------------


def test_the_public_module_names_what_a_plugin_needs() -> None:
    import privyx.plugins as api
    from privyx.core.result import Detection
    from privyx.privacy.operator.base import restore
    from privyx.vault.base import BaseVault

    assert api.Detection is Detection
    assert api.restore is restore
    assert api.BaseVault is BaseVault
    assert all(hasattr(api, name) for name in api.__all__)
    assert {"BaseDetector", "BaseVault", "ComponentConfig", "BuildContext"} <= set(api.__all__)
    with pytest.raises(AttributeError):
        _ = api.Nothing  # type: ignore[attr-defined]


def test_importing_privyx_loads_no_web_stack() -> None:
    code = (
        "import sys, privyx, privyx.plugins; from privyx import Privyx; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'starlette', 'uvicorn')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# --- Config and BuildContext ----------------------------------------------


def test_a_config_validates_the_plugin_options(tmp_path: Path) -> None:
    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}))

    detector = build_detector({"type": "acme_id", "region": "US"})
    assert detector.config.region == "US"  # type: ignore[attr-defined]
    # The flat built-in fields of the section are not the plugin's.
    assert build_detector({"type": "acme_id", "terms": {"X": ["y"]}}).config.prefix == "ACME-"  # type: ignore[attr-defined]


def test_a_misspelled_plugin_option_names_the_key_not_the_value(tmp_path: Path) -> None:
    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}))

    with pytest.raises(ConfigError) as caught:
        build_detector({"type": "acme_id", "regoin": "sk-" + "secret-value"})
    assert str(caught.value) == (
        "unknown setting 'detector.regoin' (did you mean 'region'?) for detector type 'acme_id'"
    )
    with pytest.raises(ConfigError, match=r"invalid setting 'detector.region'") as caught:
        build_detector({"type": "acme_id", "region": "sk-" + "secret-value"})
    assert "secret-value" not in str(caught.value)


def test_an_operator_gets_the_live_objects_as_a_build_context(tmp_path: Path) -> None:
    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}))
    codec = FormatCodec.default()

    operator = build_operator({"type": "seen"}, codec=codec, id_length=20)

    assert operator.ctx.codec is codec  # type: ignore[attr-defined]
    assert operator.ctx.anchor is None  # type: ignore[attr-defined]
    assert operator.ctx.id_length == 20  # type: ignore[attr-defined]


async def test_a_plugin_vault_gets_none_of_the_builtin_defaults(tmp_path: Path) -> None:
    # vault.dsn and vault.redis_url come from the defaults; a strict Config
    # would refuse them.
    paths = [str(_write(tmp_path, "v.py", STRICT_VAULT))]
    settings = load_config(
        _config(tmp_path, {"plugins": {"paths": paths}, "vault": {"type": "shelf", "shelf": "a"}})
    )
    load_plugins(settings)

    vault, _ = await build_vault(settings)

    assert vault.config.shelf == "a"  # type: ignore[attr-defined]


# --- merging a section whose type changes -----------------------------------


def test_a_layer_that_changes_type_drops_the_plugin_options_below(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    (home / ".privyx").mkdir()
    (home / ".privyx" / "config.yaml").write_text(
        json.dumps({"detector": {"type": "acme_id", "region": "EU"}}), encoding="utf-8"
    )
    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")

    settings = load_config(extra={"detector": {"type": "regex"}})

    assert settings.detector.type == "regex"  # type: ignore[union-attr]
    assert "region" not in (settings.detector.model_extra or {})  # type: ignore[union-attr]


def test_a_layer_that_keeps_the_type_keeps_the_plugin_options(home: Path) -> None:
    (home / ".privyx").mkdir()
    (home / ".privyx" / "config.yaml").write_text(
        json.dumps({"detector": {"type": "acme_id", "region": "EU", "terms": {"P": ["kestrel"]}}}),
        encoding="utf-8",
    )

    kept = load_config(extra={"detector": {"type": "acme_id", "prefix": "X"}}).detector
    regex = load_config(extra={"detector": {"type": "regex"}}).detector

    assert kept.model_extra == {"region": "EU", "prefix": "X"}  # type: ignore[union-attr]
    # Fields of the section itself still merge, as in earlier releases.
    assert regex.terms == {"P": ["kestrel"]}  # type: ignore[union-attr]


def test_options_below_the_default_type_are_kept_for_the_type_above(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file holds the options; the environment picks the plugin.
    monkeypatch.setenv("PRIVYX_VAULT", "shelf")
    settings = load_config(_config(tmp_path, {"vault": {"shelf": "a"}}))
    assert settings.vault.type == "shelf"
    assert settings.vault.model_extra == {"shelf": "a"}


DEFAULTED_VAULT = """
from privyx.plugins import BaseVault, ComponentConfig


class TimedVault(BaseVault):
    name = "timed"

    class Config(ComponentConfig):
        ttl: int = 86400
        dsn: str

    def __init__(self, config):
        self.config = config

    @classmethod
    def from_config(cls, config, ctx):
        return cls(config)

    async def create(self, session): ...
    async def get(self, session_id): ...
    async def save(self, session): ...
    async def delete(self, session_id): ...
"""

KEYED_PROVIDER = """
from privyx.plugins import BaseProvider, ComponentConfig


class CloudProvider(BaseProvider):
    name = "cloud"

    class Config(ComponentConfig):
        api_key: str = ""

    def __init__(self, config):
        self.config = config

    @classmethod
    def from_config(cls, config, ctx):
        return cls(config)

    async def send(self, payload, session_id=None):
        return payload

    async def stream(self, payload, session_id=None):
        yield payload
"""


async def test_a_config_field_named_like_a_section_field_keeps_its_own_default(
    tmp_path: Path,
) -> None:
    paths = [str(_write(tmp_path, "v.py", DEFAULTED_VAULT))]
    plain = {"plugins": {"paths": paths}, "vault": {"type": "timed"}}
    unset = load_config(_config(tmp_path, plain))
    load_plugins(unset)
    # vault.dsn has a built-in default; it is not this plugin's value.
    with pytest.raises(ConfigError, match="invalid setting 'vault.dsn'"):
        await build_vault(unset)

    given = {"plugins": {"paths": paths}, "vault": {"type": "timed", "dsn": "shelf://a"}}
    vault, _ = await build_vault(load_config(_config(tmp_path, given)))
    assert (vault.config.ttl, vault.config.dsn) == (86400, "shelf://a")  # type: ignore[attr-defined]

    # A value the user writes counts, even when it is the built-in default.
    builtin = Settings().vault.dsn
    same = {"plugins": {"paths": paths}, "vault": {"type": "timed", "dsn": builtin}}
    vault, _ = await build_vault(load_config(_config(tmp_path, same)))
    assert vault.config.dsn == builtin  # type: ignore[attr-defined]


def test_a_provider_config_keeps_its_default_without_a_key(tmp_path: Path) -> None:
    from privyx.providers.registry import build_provider

    paths = [str(_write(tmp_path, "p.py", KEYED_PROVIDER))]
    settings = Settings(plugins={"paths": paths}, provider={"type": "cloud"})
    load_plugins(settings)
    assert build_provider(settings).config.api_key == ""  # type: ignore[attr-defined]


def test_doctor_loads_plugins_from_modules(bundle: str) -> None:
    from privyx.cli.commands.doctor import _check_plugins

    ok, detail = _check_plugins(Settings(plugins={"modules": [bundle]}))
    assert ok and "bundle" in detail


# --- the schema -------------------------------------------------------------


def _valid(schema: dict[str, Any], data: dict[str, Any]) -> bool:
    jsonschema = pytest.importorskip("jsonschema")
    return bool(jsonschema.Draft202012Validator(schema).is_valid(data))


def test_the_schema_checks_each_builtin_type_and_leaves_plugins_open() -> None:
    schema = json_schema()

    assert not _valid(schema, {"detector": {"type": "regex", "regoin": "EU"}})
    assert not _valid(schema, {"vault": {"dsnn": "x"}})  # the default type is built in
    assert not _valid(schema, {"detector": [{"type": "regex"}, {"type": "yaml", "regoin": 1}]})
    assert _valid(schema, {"detector": {"type": "acme_id", "region": "EU"}})
    assert _valid(schema, {"detector": {"type": "${DETECTOR}", "region": "EU"}})


def test_the_schema_with_loaded_plugins_closes_type_and_checks_their_options(
    tmp_path: Path,
) -> None:
    from privyx.plugins.loader import loaded_options

    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}))
    schema = json_schema(loaded_options())

    assert _valid(schema, {"detector": {"type": "acme_id", "region": "US", "cache": False}})
    assert not _valid(schema, {"detector": {"type": "acme_id", "regoin": "US"}})
    assert not _valid(schema, {"detector": {"type": "acme_id", "region": "Mars"}})
    assert not _valid(schema, {"detector": {"type": "acme"}})  # a misspelled type
    assert _valid(schema, {"detector": {"type": "${DETECTOR}"}})
    assert _valid(schema, {"operator": {"type": "seen"}})


def test_a_plugin_named_like_a_builtin_type_does_not_change_its_schema(tmp_path: Path) -> None:
    from privyx.plugins.loader import loaded_options

    source = KEYED_PROVIDER.replace('name = "cloud"', 'name = "openai"')
    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", source))]}))
    schema = json_schema(loaded_options())
    # The built-in provider is what `type: openai` selects at runtime.
    assert _valid(schema, {"provider": {"type": "openai", "openai_api_key": "x"}})
    enum = schema["$defs"]["ProviderConfig"]["properties"]["type"]["anyOf"][0]["enum"]
    assert enum.count("openai") == 1


def test_config_schema_adds_the_plugins_the_config_loads(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from privyx.cli.main import cli

    config = _config(tmp_path, {"plugins": {"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}})
    plain = CliRunner().invoke(cli, ["config", "--schema"])
    loaded = CliRunner().invoke(cli, ["config", "--schema", "-c", str(config)])

    assert json.loads(plain.output) == json_schema()
    detector = json.loads(loaded.output)["$defs"]["DetectorConfig"]
    assert detector["properties"]["type"]["anyOf"][0]["enum"][0] == "llm"
    assert "acme_id" in detector["properties"]["type"]["anyOf"][0]["enum"]


def test_config_schema_asks_no_password_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    import privyx.config.loader as loader
    from privyx.cli.main import cli

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a secret was resolved")

    monkeypatch.setattr(loader, "resolve_secrets", refuse)
    paths = [str(_write(tmp_path, "p.py", CONFIGURED))]
    config = _config(
        tmp_path, {"plugins": {"paths": paths}, "provider": {"api_key": "op://vault/item/key"}}
    )
    result = CliRunner().invoke(cli, ["config", "--schema", "-c", str(config)])

    detector = json.loads(result.output)["$defs"]["DetectorConfig"]
    assert "acme_id" in detector["properties"]["type"]["anyOf"][0]["enum"]


# --- plugins.modules ----------------------------------------------------------

BUNDLE_INIT = """
from fastapi import Request

from .detector import BundleDetector

EVENTS = []


def setup(px):
    px.add(BundleDetector)

    @px.router.get("/acme/ping")
    async def ping(request: Request):
        return {"engine": type(request.app.state.engine).__name__}

    @px.on_startup
    def started():
        EVENTS.append("started")

    @px.on_shutdown
    async def stopped():
        EVENTS.append("stopped")
"""

BUNDLE_DETECTOR = """
from privyx.plugins import BaseDetector, Detection


class BundleDetector(BaseDetector):
    name = "bundle"

    def detect_sync(self, text, context):
        found = Detection()
        at = text.find("kestrel")
        if at != -1:
            found.add(at, at + 7, "PROJECT", "kestrel")
        return found
"""


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """An importable package ``acme_bundle`` that a relative import holds together."""
    package = tmp_path / "pkgs" / "acme_bundle"
    _write(package, "__init__.py", BUNDLE_INIT)
    _write(package, "detector.py", BUNDLE_DETECTOR)
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    yield "acme_bundle"
    for name in [m for m in sys.modules if m.split(".")[0] == "acme_bundle"]:
        del sys.modules[name]


async def test_a_module_sets_up_components_routes_and_hooks(bundle: str) -> None:
    px = Privyx()
    hooks = load_plugins(Settings(plugins={"modules": [bundle]}), app=px)
    assert "bundle" in PLUGINS.detectors

    await hooks.run_startup()
    await hooks.run_shutdown()
    assert sys.modules[bundle].EVENTS == ["started", "stopped"]
    assert len(px.router.routes) == 1


def test_a_module_is_set_up_once_per_privyx(bundle: str) -> None:
    px = Privyx()
    settings = Settings(plugins={"modules": [bundle]})
    load_plugins(settings, app=px)
    load_plugins(settings, app=px)  # a duplicate registration would raise

    assert len(px.router.routes) == 1


def test_modules_come_from_the_environment_beside_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRIVYX_PLUGIN_PATHS", str(tmp_path))
    monkeypatch.setenv("PRIVYX_PLUGIN_MODULES", "acme_one, acme_two")

    plugins = load_config(_config(tmp_path, {"plugins": {"modules": ["other"]}})).plugins

    assert plugins.paths == [str(tmp_path)]
    assert plugins.modules == ["acme_one", "acme_two"]


def test_a_module_without_setup_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "acme_nosetup.py", "X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ConfigError, match=r"'acme_nosetup' has no setup\(px\)"):
        load_plugins(Settings(plugins={"modules": ["acme_nosetup"]}))
    with pytest.raises(ConfigError, match="failed to import plugin module 'acme_missing'"):
        load_plugins(Settings(plugins={"modules": ["acme_missing"]}))


def test_disabled_plugins_skip_modules() -> None:
    load_plugins(Settings(plugins={"enabled": False, "modules": ["acme_missing"]}))


def test_one_name_from_two_places_stops_startup(bundle: str, tmp_path: Path) -> None:
    px = Privyx()
    source = BUNDLE_DETECTOR.replace("BundleDetector", "OtherDetector")
    paths = [str(_write(tmp_path, "d.py", source))]
    settings = Settings(plugins={"paths": paths, "modules": [bundle]})

    with pytest.raises(ConfigError, match="duplicate plugin name 'bundle'"):
        load_plugins(settings, app=px)


def test_add_takes_only_a_component_class() -> None:
    with pytest.raises(ConfigError, match="not a concrete subclass"):
        Privyx().add(dict)


# --- routes -----------------------------------------------------------------


def _router(path: str) -> APIRouter:
    router = APIRouter()
    router.add_api_route(path, lambda: {"ok": True})
    return router


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages",
        "/v1/v1/messages",
        "/{version}/messages",
        "/v1/{rest:path}",
        "/v1beta/models/{name}",
        "/v1beta/models/{model}:generateContent",
        "/{everything:path}",
        # Gemini's /*/cachedContents: the `*` is any API version.
        "/acme/{item}",
        # /v1/v1/... is masked as the route it means.
        "/{tenant}/v1/chat/completions",
        "/v1/{x}/messages",
    ],
)
def test_a_route_over_a_masking_route_stops_startup(path: str) -> None:
    with pytest.raises(ConfigError, match="overlaps a masking route"):
        include_router(FastAPI(), _router(path), Settings().proxy.routes)


def test_a_nested_router_is_checked_and_later_routes_are_not_served() -> None:
    outer = APIRouter()
    outer.include_router(_router("/models/{name}"), prefix="/v1beta")
    with pytest.raises(ConfigError, match="overlaps a masking route"):
        include_router(FastAPI(), outer, Settings().proxy.routes)


async def test_a_route_added_after_the_check_is_not_served() -> None:
    app, router = FastAPI(), _router("/acme/items/{item}")
    include_router(app, router, Settings().proxy.routes)
    router.add_api_route("/v1/messages", lambda: {"ok": True}, methods=["POST"])
    assert (await _get(app, "/acme/items/1")).status_code == 200
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/messages")).status_code == 404


async def test_a_route_beside_the_masking_routes_is_served() -> None:
    app = FastAPI()
    include_router(app, _router("/acme/items/{item}"), Settings().proxy.routes)
    assert (await _get(app, "/acme/items/1")).json() == {"ok": True}


async def test_a_masking_path_never_reaches_a_plugin_route_the_check_missed() -> None:
    # The startup check tries sample paths; none of them is an int for this route.
    app, router = FastAPI(), APIRouter()
    router.add_api_route("/api/{item:int}", lambda item: {"item": item}, methods=["POST"])
    include_router(app, router, {"/api/7*": "openai"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/12")).json() == {"item": 12}
        assert (await client.post("/api/71")).status_code == 404  # the proxy's, not the plugin's


def test_the_route_check_stays_fast_for_a_large_router() -> None:
    import time

    router = APIRouter()
    for i in range(200):
        router.add_api_route(f"/acme{i}/part{i}/{{item}}/more{i}", lambda: {"ok": True})
    start = time.perf_counter()
    include_router(FastAPI(), router, Settings().proxy.routes)
    assert time.perf_counter() - start < 5


async def _get(app: Any, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_both_apps_serve_plugin_routes_before_their_own() -> None:
    from privyx.cli.commands.proxy import build_transparent_proxy

    router = APIRouter()

    @router.get("/acme/status")
    async def status(request: Request) -> dict[str, str]:
        return {"mode": request.app.state.settings.proxy.mode}

    # A plugin cannot take /health from Privyx.
    router.add_api_route("/health", lambda: {"status": "plugin"})

    settings = Settings(audit={"enabled": False})
    engine, close = await build_engine(settings)
    proxy = build_transparent_proxy(settings, engine, None)
    transparent = create_transparent_app(engine, proxy, settings, router)
    gateway_settings = Settings(proxy={"mode": "gateway"}, audit={"enabled": False})
    provider = GenericProvider(base_url="https://up.test/v1/chat/completions")
    gateway = Gateway(engine, provider, gateway_settings, router=router).app
    try:
        assert (await _get(transparent, "/acme/status")).json() == {"mode": "transparent"}
        assert (await _get(gateway, "/acme/status")).json() == {"mode": "gateway"}
        assert (await _get(transparent, "/health")).json() == {"status": "ok"}
        assert transparent.state.engine is engine and gateway.state.engine is engine
    finally:
        await proxy.close()
        await provider.close()
        await close()


# --- Privyx -----------------------------------------------------------------


async def test_engine_uses_the_components_added_in_code(tmp_path: Path) -> None:
    from privyx.plugins import BaseDetector, Detection

    config = _config(tmp_path, {"detector": [{"type": "regex"}, {"type": "codeword"}]})
    px = Privyx(config, audit={"enabled": False}, anchor={"secret": ""})  # counter tokens

    @px.add
    class CodewordDetector(BaseDetector):
        name = "codeword"

        def detect_sync(self, text: str, context: Any) -> Detection:
            found = Detection()
            if (at := text.find("kestrel")) != -1:
                found.add(at, at + 7, "PROJECT", "kestrel")
            return found

    async with px.engine() as engine:
        session = await engine.get_or_create_session()
        masked = await engine.transform("kestrel to ana@example.com", session=session)
        restored = await engine.restore(masked.text, session.session_id)

    assert masked.text == "<PRIVYX_PROJECT_1> to <PRIVYX_EMAIL_2>"
    assert restored.text == "kestrel to ana@example.com"


def test_overrides_win_over_the_config_file(tmp_path: Path) -> None:
    px = Privyx(_config(tmp_path, {"port": 9001, "log_level": "debug"}), port=9002)
    settings = px._settings()
    assert (settings.port, settings.log_level) == (9002, "debug")


def test_run_serves_the_proxy_with_routes_and_hooks(
    bundle: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import privyx.cli.commands.proxy as proxy_cmd

    answers: list[Any] = []

    async def serve(server: Any, event: Any) -> None:
        answers.append((await _get(server.config.app, "/acme/ping")).json())

    monkeypatch.setattr(proxy_cmd, "_serve_until_reload", serve)
    config = _config(tmp_path, {"plugins": {"modules": [bundle]}, "audit": {"enabled": False}})

    Privyx(config).run()

    assert answers == [{"engine": "PrivacyEngine"}]
    assert sys.modules[bundle].EVENTS == ["started", "stopped"]
