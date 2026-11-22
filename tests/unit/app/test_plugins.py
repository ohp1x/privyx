"""Plugins: ``setup(app)``, a component's ``Options``, and the app's registries."""

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

from privyx import load_config
from privyx.app import Privyx
from privyx.app._build import (
    build_anchor,
    build_detector,
    build_engine,
    build_operator,
    build_policy,
    build_provider,
    open_vault,
)
from privyx.app._plugins import load_plugins
from privyx.app._schema import json_schema
from privyx.config.schema import Settings
from privyx.core import ConfigError, Context, ListenerError
from privyx.detectors import Detector
from privyx.events import AppStarting, AppStopping
from privyx.providers import GenericProvider
from privyx.proxy._asgi import create_app
from privyx.proxy._plugin_routes import include_router
from privyx.testing import services
from privyx.token.codec import FormatCodec

# A detector with options, an operator that keeps what it was opened with, and
# the setup(app) that adds them.
CONFIGURED = """
from typing import Literal

from privyx.core import Detection
from privyx.detectors import Detector
from privyx.operators import Operator


class AcmeIdDetector(Detector):
    name = "acme_id"

    class Options(Detector.Options):
        region: Literal["EU", "US"] = "EU"
        prefix: str = "ACME-"

    def detect_sync(self, text, context):
        found = Detection()
        at = text.find(self.options.prefix)
        if at != -1:
            found.add(at, at + len(self.options.prefix) + 4, "ACME_ID", text[at : at + 9])
        return found


class SeenOperator(Operator):
    name = "seen"

    async def open(self, services):
        await super().open(services)
        self.services = services

    async def pseudonymize(self, text, detection, session, context):
        raise NotImplementedError

    async def deanonymize(self, text, session, context):
        raise NotImplementedError


def setup(app):
    app.add(AcmeIdDetector)
    app.add(SeenOperator)
"""

STRICT_VAULT = """
from privyx.vaults import Vault


class ShelfVault(Vault):
    name = "shelf"

    class Options(Vault.Options):
        shelf: str

    async def open(self, services):
        self.opened = True

    async def close(self):
        self.closed = True

    async def create(self, session): ...
    async def get(self, session_id): ...
    async def save(self, session, *, lock=None): ...
    async def delete(self, session_id): ...


def setup(app):
    app.add(ShelfVault)
"""


#: The dialects whose paths may come without their API version.
VERSIONLESS = frozenset({"gemini"})


def _write(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(source, encoding="utf-8")
    return directory


def _config(tmp_path: Path, data: dict[str, Any]) -> Path:
    file = tmp_path / "privyx.yaml"
    file.write_text(json.dumps(data), encoding="utf-8")
    return file


def _loaded(tmp_path: Path, source: str = CONFIGURED, **settings: Any) -> Privyx:
    paths = [str(_write(tmp_path, "p.py", source))]
    return load_plugins(Settings(plugins={"paths": paths}, **settings))


# --- the public modules -----------------------------------------------------


@pytest.mark.parametrize(
    "package",
    [
        "privyx.vaults",
        "privyx.anchors",
        "privyx.policies",
        "privyx.providers",
        "privyx.detectors",
        "privyx.operators",
        "privyx.testing",
    ],
)
def test_each_kind_package_names_its_public_classes(package: str) -> None:
    import importlib

    module = importlib.import_module(package)
    assert all(hasattr(module, name) for name in module.__all__)
    with pytest.raises(AttributeError):
        _ = module.Nothing


def test_the_old_plugin_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        import privyx.plugins  # noqa: F401


def test_importing_privyx_loads_no_web_stack() -> None:
    code = (
        "import sys, privyx, privyx.vaults, privyx.detectors; from privyx import Privyx; "
        "Privyx(); print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('fastapi', 'starlette', 'uvicorn')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# --- plugins.paths ------------------------------------------------------------

KINDS = """
from privyx.anchors import Anchor
from privyx.core import Detection, TransformResult
from privyx.detectors import Detector
from privyx.operators import Operator
from privyx.policies import Policy
from privyx.providers import Provider


class LicensePlateDetector(Detector):
    name = "license_plate"

    def detect_sync(self, text, context):
        found = Detection()
        if (at := text.find("PLATE")) != -1:
            found.add(at, at + 5, "LICENSE_PLATE", "PLATE")
        return found


class UppercaseOperator(Operator):
    name = "uppercase"

    async def pseudonymize(self, text, detection, session, context):
        return TransformResult(text=text.upper())

    async def deanonymize(self, text, session, context):
        return TransformResult(text=text.lower())


class AllowAllPolicy(Policy):
    name = "allow_all"

    def decide_sync(self, detection, context):
        return detection


class EchoProvider(Provider):
    name = "echo"

    def url(self, path, *, stream):
        return "https://echo.example.com" + path


class NoopAnchor(Anchor):
    name = "noop"

    async def anchor(self, text, context):
        return text


def setup(app):
    for cls in (LicensePlateDetector, UppercaseOperator, AllowAllPolicy, EchoProvider, NoopAnchor):
        app.add(cls)
"""


async def test_a_file_adds_a_component_of_each_kind(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "kinds.py", KINDS))]
    settings = Settings(
        plugins={"paths": paths},
        detectors={"license_plate": {"type": "license_plate"}},
        operator={"type": "uppercase"},
        policy={"type": "allow_all"},
        provider={"type": "echo"},
        anchor={"type": "noop"},
    )
    app = load_plugins(settings)

    detection = await build_detector(settings.detectors, app).detect("car PLATE", Context())
    assert [s.entity_type for s in detection.spans] == ["LICENSE_PLATE"]
    assert type(build_operator(settings, app)).name == "uppercase"
    assert type(build_policy(settings, app)).name == "allow_all"
    assert (
        build_provider(settings, app).url("/v1/x", stream=False) == "https://echo.example.com/v1/x"
    )
    assert type(build_anchor(settings, app)).name == "noop"


def test_a_file_without_setup_stops_startup(tmp_path: Path) -> None:
    source = KINDS.split("def setup(app):")[0]
    with pytest.raises(ConfigError, match=r"plugin \S+kinds\.py has no setup\(app\) function"):
        load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "kinds.py", source))]}))


def test_a_class_is_found_only_by_its_setup(tmp_path: Path) -> None:
    source = KINDS.replace("for cls in (LicensePlateDetector,", "for cls in (")
    app = load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "kinds.py", source))]}))
    assert "license_plate" not in app.registry(Detector)


def test_two_components_with_one_name_stop_startup(tmp_path: Path) -> None:
    _write(tmp_path, "one.py", KINDS)
    _write(tmp_path, "two.py", KINDS)
    with pytest.raises(ConfigError, match="two detector plugins are named 'license_plate'"):
        load_plugins(Settings(plugins={"paths": [str(tmp_path)]}))


def test_a_missing_path_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="plugin path does not exist"):
        load_plugins(Settings(plugins={"paths": [str(tmp_path / "nope")]}))


def test_a_broken_file_stops_startup(tmp_path: Path) -> None:
    source = 'raise RuntimeError("boom on import")\n'
    with pytest.raises(ConfigError, match="failed to import plugin"):
        load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "p.py", source))]}))


def test_underscore_files_are_skipped_and_a_file_can_be_named(tmp_path: Path) -> None:
    _write(tmp_path, "_helper.py", 'raise RuntimeError("imported")\n')
    _write(tmp_path, "kinds.py", KINDS)
    for path in (tmp_path, tmp_path / "kinds.py"):
        app = load_plugins(Settings(plugins={"paths": [str(path)]}))
        assert "license_plate" in app.registry(Detector)


def test_disabled_or_no_paths_load_nothing(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "kinds.py", KINDS))]
    for plugins in ({"enabled": False, "paths": paths}, {"paths": []}):
        app = load_plugins(Settings(plugins=plugins))
        assert "license_plate" not in app.registry(Detector)


def test_loading_again_into_one_app_adds_nothing(tmp_path: Path) -> None:
    settings = Settings(plugins={"paths": [str(_write(tmp_path, "kinds.py", KINDS))]})
    app = load_plugins(settings)
    assert load_plugins(settings, app) is app  # a second add of the same name would raise


def test_two_apps_do_not_see_each_others_plugins(tmp_path: Path) -> None:
    load_plugins(Settings(plugins={"paths": [str(_write(tmp_path, "kinds.py", KINDS))]}))
    assert "license_plate" not in Privyx().registry(Detector)


def test_a_plugin_anchor_without_a_secret_is_built_with_an_empty_one(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "kinds.py", KINDS))]
    settings = Settings(plugins={"paths": paths}, anchor={"type": "noop", "secret": ""})
    assert type(build_anchor(settings, load_plugins(settings))).name == "noop"


SECRET_ANCHOR = """
from privyx.anchors import Anchor


class SecretAnchor(Anchor):
    name = "secret"

    class Options(Anchor.Options):
        secret: str = "auto"

    async def anchor(self, text, context):
        return self.options.secret + text


def setup(app):
    app.add(SecretAnchor)
"""


def test_a_plugin_anchor_gets_its_secret_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import privyx.security.keys as keys

    def refuse() -> str:
        raise AssertionError("the per-user key was read")

    monkeypatch.setattr(keys, "read_or_create_anchor_secret", refuse)
    paths = [str(_write(tmp_path, "s.py", SECRET_ANCHOR))]
    auto = Settings(plugins={"paths": paths}, anchor={"type": "secret"})
    anchor = build_anchor(auto, load_plugins(auto))
    assert anchor is not None and anchor.options.secret == "auto"  # type: ignore[attr-defined]
    off = Settings(plugins={"paths": paths}, anchor={"type": "secret", "secret": ""})
    assert build_anchor(off, load_plugins(off)) is None  # an empty secret is no anchor


async def test_a_plugin_vault_is_opened_and_closed(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "v.py", STRICT_VAULT))]
    settings = Settings(plugins={"paths": paths}, vault={"type": "shelf", "shelf": "a"})
    vault, close = await open_vault(settings, load_plugins(settings))
    assert vault.opened and vault.options.shelf == "a"  # type: ignore[attr-defined]
    await close()
    assert vault.closed  # type: ignore[attr-defined]


# --- Options ------------------------------------------------------------------


def test_options_check_the_plugin_options(tmp_path: Path) -> None:
    app = _loaded(tmp_path)

    detector = build_detector({"acme_id": {"region": "US", "cache": False}}, app)
    assert detector.options.region == "US"  # type: ignore[attr-defined]
    # The options of the built-in types are not the plugin's: left out, not refused.
    detector = build_detector({"acme_id": {"terms": {"X": ["y"]}, "cache": False}}, app)
    assert type(detector).name == "acme_id"


def test_a_misspelled_plugin_option_names_the_key_not_the_value(tmp_path: Path) -> None:
    app = _loaded(tmp_path)

    with pytest.raises(ConfigError) as caught:
        build_detector({"acme_id": {"regoin": "sk-" + "secret-value"}}, app)
    assert str(caught.value) == (
        "unknown setting 'detectors.acme_id.regoin' (did you mean 'region'?) "
        "for detector type 'acme_id'"
    )
    with pytest.raises(ConfigError, match=r"invalid setting 'detectors.acme_id.region'") as caught:
        build_detector({"acme_id": {"region": "sk-" + "secret-value"}}, app)
    assert "secret-value" not in str(caught.value)


async def test_an_operator_is_opened_with_what_the_app_shares(tmp_path: Path) -> None:
    app = _loaded(tmp_path)
    codec = FormatCodec.default()
    operator = build_operator(Settings(operator={"type": "seen"}), app)
    await operator.open(services(codec=codec))

    assert operator.services.codec is codec  # type: ignore[attr-defined]
    assert operator.services.anchor is None  # type: ignore[attr-defined]


async def test_a_plugin_vault_gets_none_of_the_builtin_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the Docker image sets for every vault type: not the plugin's.
    monkeypatch.setenv("PRIVYX_VAULT_DSN", "sqlite+aiosqlite:////data/privyx.db")
    paths = [str(_write(tmp_path, "v.py", STRICT_VAULT))]
    settings = load_config(
        _config(tmp_path, {"plugins": {"paths": paths}, "vault": {"type": "shelf", "shelf": "a"}})
    )
    vault, _ = await open_vault(settings, load_plugins(settings))

    assert vault.options.shelf == "a"  # type: ignore[attr-defined]


# --- merging a section whose type changes -----------------------------------


def test_a_layer_that_changes_type_drops_the_plugin_options_below(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    (home / ".privyx").mkdir()
    (home / ".privyx" / "config.yaml").write_text(
        json.dumps({"detectors": {"own": {"type": "acme_id", "region": "EU"}}}), encoding="utf-8"
    )
    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")

    own = load_config(extra={"detectors": {"own": {"type": "regex"}}}).detectors["own"]

    assert own.type == "regex"
    assert "region" not in (own.model_extra or {})


def test_a_layer_that_keeps_the_type_keeps_the_plugin_options(home: Path) -> None:
    (home / ".privyx").mkdir()
    (home / ".privyx" / "config.yaml").write_text(
        json.dumps(
            {"detectors": {"own": {"type": "acme_id", "region": "EU", "terms": {"P": ["kestrel"]}}}}
        ),
        encoding="utf-8",
    )

    kept = load_config(extra={"detectors": {"own": {"type": "acme_id", "prefix": "X"}}})
    regex = load_config(extra={"detectors": {"own": {"type": "regex"}}})

    # The terms stay as well: a built-in type's option, which the plugin is not given.
    assert kept.detectors["own"].model_extra == {
        "region": "EU",
        "prefix": "X",
        "terms": {"P": ["kestrel"]},
    }
    # Options some built-in type takes still merge, as in earlier releases.
    assert regex.detectors["own"].model_dump()["terms"] == {"P": ["kestrel"]}


def test_options_below_the_default_type_are_kept_for_the_type_above(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file holds the options; the environment picks the plugin.
    monkeypatch.setenv("PRIVYX_VAULT", "shelf")
    settings = load_config(_config(tmp_path, {"vault": {"shelf": "a"}}))
    assert settings.vault.type == "shelf"
    assert settings.vault.model_extra == {"shelf": "a"}


DEFAULTED_VAULT = """
from privyx.vaults import Vault


class TimedVault(Vault):
    name = "timed"

    class Options(Vault.Options):
        ttl: int = 86400
        dsn: str

    async def create(self, session): ...
    async def get(self, session_id): ...
    async def save(self, session, *, lock=None): ...
    async def delete(self, session_id): ...


def setup(app):
    app.add(TimedVault)
"""

KEYED_PROVIDER = """
from privyx.providers import Provider


class CloudProvider(Provider):
    name = "cloud"

    def url(self, path, *, stream):
        return "https://cloud.example.com" + path


def setup(app):
    app.add(CloudProvider)
"""


async def test_an_option_named_like_a_builtin_one_keeps_its_own_default(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "v.py", DEFAULTED_VAULT))]
    plain = {"plugins": {"paths": paths}, "vault": {"type": "timed"}}
    unset = load_config(_config(tmp_path, plain))
    # vault.dsn has a built-in default; it is not this plugin's value.
    with pytest.raises(ConfigError, match="invalid setting 'vault.dsn'"):
        await open_vault(unset, load_plugins(unset))

    given = {"plugins": {"paths": paths}, "vault": {"type": "timed", "dsn": "shelf://a"}}
    settings = load_config(_config(tmp_path, given))
    vault, _ = await open_vault(settings, load_plugins(settings))
    assert (vault.options.ttl, vault.options.dsn) == (86400, "shelf://a")  # type: ignore[attr-defined]


def test_a_provider_gets_the_resolved_url_and_key(tmp_path: Path) -> None:
    paths = [str(_write(tmp_path, "p.py", KEYED_PROVIDER))]
    settings = Settings(plugins={"paths": paths}, provider={"type": "cloud", "api_key": "k"})
    provider = build_provider(settings, load_plugins(settings))
    assert provider.options.api_key == "k"
    assert provider.options.base_url == "http://localhost:20128"  # the generic default


def test_doctor_loads_plugins_from_modules(bundle: str) -> None:
    from privyx.app._services._diagnostics import _check_plugins

    ok, detail = _check_plugins(Settings(plugins={"modules": [bundle]}), Privyx())
    assert ok and "bundle" in detail


# --- the schema -------------------------------------------------------------


def _valid(schema: dict[str, Any], data: dict[str, Any]) -> bool:
    jsonschema = pytest.importorskip("jsonschema")
    return bool(jsonschema.Draft202012Validator(schema).is_valid(data))


def test_the_schema_checks_each_builtin_type_and_leaves_plugins_open() -> None:
    schema = json_schema()

    assert not _valid(schema, {"detectors": {"regex": {"regoin": "EU"}}})
    assert not _valid(schema, {"vault": {"dsnn": "x"}})  # the default type is built in
    assert not _valid(schema, {"detectors": {"own": {"type": "regex", "regoin": 1}}})
    assert _valid(schema, {"detectors": {"own": {"type": "acme_id", "region": "EU"}}})
    assert _valid(schema, {"detectors": {"acme_id": {"region": "EU"}}})
    assert _valid(schema, {"detectors": {"own": {"type": "${DETECTOR}", "region": "EU"}}})


def test_the_schema_with_loaded_plugins_closes_type_and_checks_their_options(
    tmp_path: Path,
) -> None:
    schema = json_schema(_loaded(tmp_path))

    assert _valid(schema, {"detectors": {"acme_id": {"region": "US", "cache": False}}})
    assert _valid(schema, {"detectors": {"own": {"type": "acme_id", "region": "US"}}})
    assert not _valid(schema, {"detectors": {"acme_id": {"regoin": "US"}}})
    assert not _valid(schema, {"detectors": {"acme_id": {"region": "Mars"}}})
    assert not _valid(schema, {"detectors": {"own": {"type": "acme"}}})  # a misspelled type
    assert _valid(schema, {"detectors": {"own": {"type": "${DETECTOR}"}}})
    assert _valid(schema, {"operator": {"type": "seen"}})


def test_a_plugin_cannot_take_a_builtin_name(tmp_path: Path) -> None:
    source = KEYED_PROVIDER.replace('name = "cloud"', 'name = "openai"')
    with pytest.raises(ConfigError, match="provider type 'openai' is built in"):
        _loaded(tmp_path, source)


def test_config_schema_adds_the_plugins_the_config_loads(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from privyx.cli.main import cli

    config = _config(tmp_path, {"plugins": {"paths": [str(_write(tmp_path, "p.py", CONFIGURED))]}})
    plain = CliRunner().invoke(cli, ["config", "schema"])
    loaded = CliRunner().invoke(cli, ["-c", str(config), "config", "schema"])

    assert json.loads(plain.output) == json_schema()
    detector = json.loads(loaded.output)["$defs"]["DetectorConfig"]
    assert detector["properties"]["type"]["anyOf"][0]["enum"][0] == "llm"
    assert "acme_id" in detector["properties"]["type"]["anyOf"][0]["enum"]


def test_config_schema_asks_no_password_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from privyx.app import Privyx
    from privyx.cli.main import cli

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a secret was resolved")

    monkeypatch.setattr(Privyx, "_resolve", refuse)
    paths = [str(_write(tmp_path, "p.py", CONFIGURED))]
    config = _config(
        tmp_path, {"plugins": {"paths": paths}, "provider": {"api_key": "op://vault/item/key"}}
    )
    result = CliRunner().invoke(cli, ["-c", str(config), "config", "schema"])

    detector = json.loads(result.output)["$defs"]["DetectorConfig"]
    assert "acme_id" in detector["properties"]["type"]["anyOf"][0]["enum"]


# --- plugins.modules ----------------------------------------------------------

BUNDLE_INIT = """
from fastapi import Request

from privyx.events import AppStarting, AppStopping

from .detector import BundleDetector

EVENTS = []


def setup(app):
    app.add(BundleDetector)

    @app.router.get("/acme/ping")
    async def ping(request: Request):
        return {"engine": type(request.app.state.engine).__name__}

    @app.on(AppStarting)
    def started(event):
        EVENTS.append(event.name)

    @app.on(AppStopping)
    async def stopped(event):
        EVENTS.append(event.name)
"""

BUNDLE_DETECTOR = """
from privyx.detectors import Detector
from privyx.core import Detection


class BundleDetector(Detector):
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


async def test_a_module_sets_up_components_routes_and_listeners(bundle: str) -> None:
    px = Privyx()
    load_plugins(Settings(plugins={"modules": [bundle]}), px)
    assert "bundle" in px.registry(Detector)

    await px._events.emit(AppStarting())
    await px._events.emit(AppStopping())
    assert sys.modules[bundle].EVENTS == ["app.starting", "app.stopping"]
    assert len(px.router.routes) == 1


def test_a_module_is_set_up_once_per_privyx(bundle: str) -> None:
    px = Privyx()
    settings = Settings(plugins={"modules": [bundle]})
    load_plugins(settings, px)
    load_plugins(settings, px)  # a duplicate registration would raise

    assert len(px.router.routes) == 1


def test_modules_come_from_the_environment_beside_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("acme_one", "acme_two"):
        _write(tmp_path / "modules", f"{name}.py", "def setup(app):\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path / "modules"))
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
    with pytest.raises(ConfigError, match=r"'acme_nosetup' has no setup\(app\)"):
        load_plugins(Settings(plugins={"modules": ["acme_nosetup"]}))
    with pytest.raises(ConfigError, match="failed to import plugin module 'acme_missing'"):
        load_plugins(Settings(plugins={"modules": ["acme_missing"]}))


def test_disabled_plugins_skip_modules() -> None:
    load_plugins(Settings(plugins={"enabled": False, "modules": ["acme_missing"]}))


def test_one_name_from_two_places_stops_startup(bundle: str, tmp_path: Path) -> None:
    px = Privyx()
    source = BUNDLE_DETECTOR.replace("BundleDetector", "OtherDetector")
    source += "\n\ndef setup(app):\n    app.add(OtherDetector)\n"
    paths = [str(_write(tmp_path / "plugins", "d.py", source))]
    settings = Settings(plugins={"paths": paths, "modules": [bundle]})

    with pytest.raises(ConfigError, match="two detector plugins are named 'bundle'"):
        load_plugins(settings, px)


def test_add_takes_only_a_component_class() -> None:
    with pytest.raises(ConfigError, match="is not a Privyx component"):
        Privyx().add(dict)  # type: ignore[type-var]


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
        # So is a Gemini path without its API version.
        "/{a}/{b}:streamGenerateContent",
    ],
)
def test_a_route_over_a_masking_route_stops_startup(path: str) -> None:
    with pytest.raises(ConfigError, match="overlaps a masking route"):
        include_router(FastAPI(), _router(path), Settings().proxy.routes, VERSIONLESS)


def test_a_nested_router_is_checked_and_later_routes_are_not_served() -> None:
    outer = APIRouter()
    outer.include_router(_router("/models/{name}"), prefix="/v1beta")
    with pytest.raises(ConfigError, match="overlaps a masking route"):
        include_router(FastAPI(), outer, Settings().proxy.routes, VERSIONLESS)


async def test_a_route_added_after_the_check_is_not_served() -> None:
    app, router = FastAPI(), _router("/acme/items/{item}")
    include_router(app, router, Settings().proxy.routes, VERSIONLESS)
    router.add_api_route("/v1/messages", lambda: {"ok": True}, methods=["POST"])
    assert (await _get(app, "/acme/items/1")).status_code == 200
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/messages")).status_code == 404


async def test_a_route_beside_the_masking_routes_is_served() -> None:
    app = FastAPI()
    include_router(app, _router("/acme/items/{item}"), Settings().proxy.routes, VERSIONLESS)
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
    include_router(FastAPI(), router, Settings().proxy.routes, VERSIONLESS)
    assert time.perf_counter() - start < 5


async def _get(app: Any, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_both_apps_serve_plugin_routes_before_their_own() -> None:
    from privyx.app._serve import build_pipeline

    router = APIRouter()

    @router.get("/acme/status")
    async def status(request: Request) -> dict[str, str]:
        return {"mode": request.app.state.settings.proxy.mode}

    # A plugin cannot take /health from Privyx.
    router.add_api_route("/health", lambda: {"status": "plugin"})

    settings = Settings()
    engine, close = await build_engine(settings)
    proxy = build_pipeline(settings, engine, GenericProvider())
    transparent = create_app(proxy, settings, router)
    gateway_settings = Settings(proxy={"mode": "gateway"})
    provider = GenericProvider(base_url="https://up.test/v1/chat/completions")
    gateway_pipeline = build_pipeline(gateway_settings, engine, provider)
    gateway = create_app(gateway_pipeline, gateway_settings, router)
    try:
        assert (await _get(transparent, "/acme/status")).json() == {"mode": "transparent"}
        assert (await _get(gateway, "/acme/status")).json() == {"mode": "gateway"}
        assert (await _get(transparent, "/health")).json() == {"status": "ok"}
        assert transparent.state.engine is engine and gateway.state.engine is engine
    finally:
        await proxy.close()
        await gateway_pipeline.close()
        await close()


# --- Privyx -----------------------------------------------------------------


async def test_engine_uses_the_components_added_in_code(tmp_path: Path) -> None:
    from privyx.core import Detection

    config = _config(tmp_path, {"detector": [{"type": "regex"}, {"type": "codeword"}]})
    px = Privyx(config, anchor={"secret": ""})  # counter tokens

    @px.add
    class CodewordDetector(Detector):
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
    config = _config(tmp_path, {"server": {"port": 9001}, "log": {"level": "debug"}})
    settings = Privyx(config, server={"port": 9002}).settings
    assert (settings.server.port, settings.log.level) == (9002, "debug")


def test_serve_serves_the_proxy_with_routes_and_listeners(
    bundle: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: list[Any] = []

    async def serve(server: Any, event: Any) -> None:
        answers.append((await _get(server.config.app, "/acme/ping")).json())

    monkeypatch.setattr("privyx.app._serve.serve_until", serve)
    config = _config(tmp_path, {"plugins": {"modules": [bundle]}, "features": {"audit": False}})

    Privyx(config).serve()

    assert answers == [{"engine": "PrivacyEngine"}]
    assert sys.modules[bundle].EVENTS == ["app.starting", "app.stopping"]


def test_a_starting_listener_that_fails_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[bool] = []

    async def serve(server: Any, event: Any) -> None:
        served.append(True)

    monkeypatch.setattr("privyx.app._serve.serve_until", serve)
    px = Privyx(_config(tmp_path, {"features": {"audit": False}}))
    stopped: list[str] = []

    @px.on(AppStarting)
    def refuse(event: AppStarting) -> None:
        raise RuntimeError("no license")

    @px.on(AppStopping)
    def stop(event: AppStopping) -> None:
        stopped.append(event.name)

    with pytest.raises(ListenerError, match="refuse failed on app.starting: no license"):
        px.serve()
    assert served == []
    assert stopped == []


# --- defaults from plugins ----------------------------------------------------

DEFAULTS_PLUGIN = """
def setup(app):
    app.defaults(
        agents={"goose": {"command": "goose", "provider": "openai", "env_vars": ["OPENAI_HOST"]}},
        presets={"quiet": {"log": {"level": "warning"}}},
    )
"""


def test_plugin_defaults_sit_below_every_config_file(tmp_path: Path) -> None:
    plugins = _write(tmp_path / "plugins", "goose.py", DEFAULTS_PLUGIN)
    config = _config(
        tmp_path, {"plugins": {"paths": [str(plugins)]}, "agents": {"goose": {"command": "g2"}}}
    )

    settings = load_config(config, agent="goose", presets=["quiet"])

    goose = settings.agents["goose"]
    assert (goose.command, goose.provider, goose.env_vars) == ("g2", "openai", ["OPENAI_HOST"])
    assert settings.log.level == "warning"


def test_plugin_defaults_cannot_take_a_built_in_profile(tmp_path: Path) -> None:
    from privyx.app import Privyx

    app = Privyx()
    with pytest.raises(ConfigError, match="agent profile 'claude' is built in"):
        app.defaults(agents={"claude": {"command": "x"}})
    app.defaults(presets={"quiet": {}})
    with pytest.raises(ConfigError, match="two plugins add presets.quiet"):
        app.defaults(presets={"quiet": {}})


# --- dialects from plugins ----------------------------------------------------

CONVERSE = """
from privyx.dialects import Dialect


class ConverseDialect(Dialect):
    name = "acme-converse"
    content_keys = frozenset({"messages", "system"})
    default_routes = ("/model/*/converse",)


def setup(app):
    app.add(ConverseDialect)
"""


def test_a_plugin_dialect_adds_its_routes_to_the_default_map(tmp_path: Path) -> None:
    from privyx.app import Privyx
    from privyx.app._serve import add_default_routes

    plugins = _write(tmp_path / "plugins", "converse.py", CONVERSE)
    app = Privyx(_config(tmp_path, {"plugins": {"paths": [str(plugins)]}}))
    settings = app.settings
    add_default_routes(app, settings)

    assert settings.proxy.routes["/model/*/converse"] == "acme-converse"
    assert settings.proxy.routes["/v1/messages"] == "anthropic"  # the default ones stay

    written = {"plugins": {"paths": [str(plugins)]}, "proxy": {"routes": {"/v1/x": "openai"}}}
    app = Privyx(_config(tmp_path, written))
    settings = app.settings
    add_default_routes(app, settings)
    assert settings.proxy.routes == {"/v1/x": "openai"}  # a written map is taken as written


def test_a_plugin_dialect_cannot_take_a_default_route(tmp_path: Path) -> None:
    from privyx.app import Privyx
    from privyx.app._serve import add_default_routes

    source = CONVERSE.replace('("/model/*/converse",)', '("/v1/messages",)')
    plugins = _write(tmp_path / "plugins", "converse.py", source)
    app = Privyx(_config(tmp_path, {"plugins": {"paths": [str(plugins)]}}))
    with pytest.raises(ConfigError, match="brings route /v1/messages, which is 'anthropic'"):
        add_default_routes(app, app.settings)
