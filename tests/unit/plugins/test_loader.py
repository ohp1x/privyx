"""Tests for the local plugin loader (auto-discovery by subclass)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from privyx.config.loader import load_config
from privyx.config.schema import Settings
from privyx.core.builder import build_anchor, build_detector_from, build_operator, build_vault
from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.plugins.loader import load_plugins
from privyx.plugins.registry import PLUGINS
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.policy.loader import build_policy
from privyx.providers.registry import build_provider

# --- plugin sources written to a temp dir --------------------------------

DETECTOR = """
from privyx.privacy.detector.base import BaseDetector
from privyx.core.result import Detection

class LicensePlateDetector(BaseDetector):
    name = "license_plate"

    def detect_sync(self, text, context):
        d = Detection()
        idx = text.find("PLATE")
        if idx != -1:
            d.add(idx, idx + 5, "LICENSE_PLATE", "PLATE")
        return d
"""

OPERATOR = """
from privyx.privacy.operator.base import BaseOperator
from privyx.core.result import TransformResult

class UppercaseOperator(BaseOperator):
    name = "uppercase"

    async def pseudonymize(self, text, detection, session, context):
        return TransformResult(text=text.upper())

    async def deanonymize(self, text, session, context):
        return TransformResult(text=text.lower())
"""

POLICY = """
from privyx.privacy.policy.base import BasePolicy

class AllowAllPolicy(BasePolicy):
    name = "allow_all"

    def _decide_sync(self, detection, context):
        return detection
"""

PROVIDER = """
from privyx.providers.base import BaseProvider

class EchoProvider(BaseProvider):
    name = "echo"

    def __init__(self, config=None):
        self.config = config or {}

    @classmethod
    def from_config(cls, config):
        return cls(config)

    async def send(self, payload, session_id=None):
        return payload

    async def stream(self, payload, session_id=None):
        yield payload
"""

ANCHOR = """
from privyx.privacy.anchor.base import BaseAnchor

class NoopAnchor(BaseAnchor):
    name = "noop"

    async def anchor(self, text, context):
        return text

    async def deanchor(self, anchored, context):
        return anchored
"""

VAULT = """
from privyx.vault.base import BaseVault

class DictVault(BaseVault):
    name = "dict"

    def __init__(self):
        self._store = {}
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def create(self, session):
        self._store[session.session_id] = session

    async def get(self, session_id):
        return self._store.get(session_id)

    async def save(self, session):
        self._store[session.session_id] = session

    async def delete(self, session_id):
        self._store.pop(session_id, None)
"""

CONFIGURED = """
from privyx.privacy.detector.base import BaseDetector
from privyx.core.result import Detection

class ConfiguredDetector(BaseDetector):
    name = "configured"

    def __init__(self, marker="X"):
        super().__init__()
        self.marker = marker

    @classmethod
    def from_config(cls, config):
        return cls(marker=config.get("marker", "X"))

    def detect_sync(self, text, context):
        return Detection()
"""

UNNAMED = """
from privyx.privacy.detector.base import BaseDetector
from privyx.core.result import Detection

class WidgetDetector(BaseDetector):
    def detect_sync(self, text, context):
        return Detection()
"""

DUPLICATE = """
from privyx.privacy.detector.base import BaseDetector
from privyx.core.result import Detection

class OneDetector(BaseDetector):
    name = "dup"
    def detect_sync(self, text, context):
        return Detection()

class TwoDetector(BaseDetector):
    name = "dup"
    def detect_sync(self, text, context):
        return Detection()
"""

BROKEN = """
raise RuntimeError("boom on import")
"""


def _write(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / name
    file.write_text(source)
    return file


def _settings(tmp_path: Path, source: str, name: str = "plug.py", **overrides: object) -> Settings:
    _write(tmp_path, name, source)
    data: dict[str, object] = {"plugins": {"enabled": True, "paths": [str(tmp_path)]}}
    data.update(overrides)
    return Settings(**data)


# --- discovery per family -------------------------------------------------


async def test_detector_plugin_discovered_and_built(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, DETECTOR))
    assert "license_plate" in PLUGINS.detectors

    detector = build_detector({"type": "license_plate"})
    assert detector.name == "license_plate"
    result = await detector.detect("car PLATE here", Context())
    assert [s.entity_type for s in result.spans] == ["LICENSE_PLATE"]


def test_operator_plugin_discovered_and_built(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, OPERATOR))
    assert "uppercase" in PLUGINS.operators
    op = build_operator({"type": "uppercase"})
    assert op.name == "uppercase"


def test_policy_plugin_discovered_and_built(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, POLICY))
    assert "allow_all" in PLUGINS.policies
    policy = build_policy({"type": "allow_all"})
    assert policy.name == "allow_all"


def test_provider_plugin_discovered_and_built(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PROVIDER, provider={"type": "echo"})
    load_plugins(settings)
    assert "echo" in PLUGINS.providers
    provider = build_provider(settings)
    assert provider.name == "echo"


def test_anchor_plugin_built_without_secret(tmp_path: Path) -> None:
    # A plugin anchor is built even with an empty secret (the hmac-only gate).
    settings = _settings(tmp_path, ANCHOR, anchor={"type": "noop", "secret": ""})
    load_plugins(settings)
    anchor = build_anchor(settings)
    assert anchor is not None
    assert anchor.name == "noop"


async def test_vault_plugin_connect_and_close(tmp_path: Path) -> None:
    settings = _settings(tmp_path, VAULT, vault={"type": "dict"})
    load_plugins(settings)
    assert "dict" in PLUGINS.vaults
    vault, closer = await build_vault(settings)
    assert vault.connected is True  # connect() was awaited
    await closer()
    assert vault.closed is True


# --- construction & naming contract --------------------------------------


def test_from_config_receives_config(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, CONFIGURED))
    detector = build_detector({"type": "configured", "marker": "Z"})
    assert detector.marker == "Z"  # type: ignore[attr-defined]


def test_plugin_options_reach_from_config(tmp_path: Path) -> None:
    # The settings model dropped every key it does not define, so a plugin
    # configured from a file never saw its own options.
    _write(tmp_path, "detector.py", CONFIGURED)
    _write(tmp_path, "provider.py", PROVIDER)
    cfg = tmp_path / "c.yaml"
    config = {
        "plugins": {"paths": [str(tmp_path)]},
        "detector": {"type": "configured", "marker": "Z", "cache": False},
        "provider": {"type": "echo", "region": "eu", "timeout": 5},
    }
    cfg.write_text(json.dumps(config), encoding="utf-8")
    settings = load_config(cfg)
    load_plugins(settings)

    assert build_detector_from(settings).marker == "Z"  # type: ignore[attr-defined]
    assert build_provider(settings).config["region"] == "eu"
    assert build_provider(settings).config["timeout"] == 5  # not proxy.timeout


def test_name_derived_when_unset(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, UNNAMED))
    assert "widget" in PLUGINS.detectors


# --- opt-in behaviour -----------------------------------------------------


def test_no_paths_loads_nothing(tmp_path: Path) -> None:
    load_plugins(_settings(tmp_path, DETECTOR))  # populate
    assert "license_plate" in PLUGINS.detectors
    load_plugins(Settings(plugins={"enabled": True, "paths": []}))  # clears
    assert PLUGINS.detectors.names() == []


def test_disabled_loads_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "plug.py", DETECTOR)
    load_plugins(Settings(plugins={"enabled": False, "paths": [str(tmp_path)]}))
    assert PLUGINS.detectors.names() == []


# --- error handling -------------------------------------------------------


def test_duplicate_name_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate plugin name 'dup'"):
        load_plugins(_settings(tmp_path, DUPLICATE))


def test_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="plugin path does not exist"):
        load_plugins(Settings(plugins={"enabled": True, "paths": [str(tmp_path / "nope")]}))


def test_broken_module_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="failed to import plugin"):
        load_plugins(_settings(tmp_path, BROKEN))


def test_underscore_files_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "_helper.py", BROKEN)  # would raise if imported
    _write(tmp_path, "plug.py", DETECTOR)
    load_plugins(Settings(plugins={"enabled": True, "paths": [str(tmp_path)]}))
    assert "license_plate" in PLUGINS.detectors


def test_single_file_path(tmp_path: Path) -> None:
    file = _write(tmp_path, "plug.py", DETECTOR)
    load_plugins(Settings(plugins={"enabled": True, "paths": [str(file)]}))
    assert "license_plate" in PLUGINS.detectors


# --- lifecycle hooks ------------------------------------------------------


async def test_startup_and_shutdown_hooks_fire(tmp_path: Path) -> None:
    source = f"""
import pathlib
FLAG = pathlib.Path({str(tmp_path / "flags")!r})

def on_startup():
    FLAG.write_text("started")

async def on_shutdown():
    FLAG.write_text("stopped")
"""
    hooks = load_plugins(_settings(tmp_path, source))
    await hooks.run_startup()
    assert (tmp_path / "flags").read_text() == "started"
    await hooks.run_shutdown()
    assert (tmp_path / "flags").read_text() == "stopped"


async def test_failing_startup_hook_raises(tmp_path: Path) -> None:
    source = """
def on_startup():
    raise RuntimeError("nope")
"""
    hooks = load_plugins(_settings(tmp_path, source))
    with pytest.raises(ConfigError, match="startup hook"):
        await hooks.run_startup()
