"""Automatic anchors share one persisted key across command invocations."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.commands.run import _run_settings
from privyx.cli.main import cli
from privyx.core.builder import build_anchor
from privyx.core.context import Context
from privyx.privacy.anchor.hmac import HMACAnchor


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("anchor:\n  secret: auto\ndetector:\n  type: regex\n")
    return path


async def test_run_and_other_commands_share_the_persisted_anchor(config: Path, home: Path) -> None:
    from privyx.config.loader import load_config

    settings = load_config(config)
    anchor = build_anchor(settings)
    assert anchor is not None
    key = home / ".privyx" / "anchor.key"
    assert key.exists()
    expected = await HMACAnchor(key.read_text()).anchor("alice@example.com", Context())
    assert await anchor.anchor("alice@example.com", Context()) == expected
    run_settings = _run_settings(None, None, None, None, False)
    run_anchor = build_anchor(run_settings)
    assert run_anchor is not None
    assert await run_anchor.anchor("alice@example.com", Context()) == expected
    assert settings.anchor.secret == "auto"  # inspection never exposes the provisioned key


async def test_run_still_provisions_the_secret_for_a_plugin_anchor(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from privyx.plugins.registry import PLUGINS, Registry
    from privyx.privacy.anchor.base import Anchor

    registry: Registry[Anchor] = Registry("anchor")
    registry.register("custom", lambda config: HMACAnchor(config["secret"]))
    monkeypatch.setattr(PLUGINS, "anchors", registry)
    settings = _run_settings(None, None, None, None, False)
    settings.anchor.type = "custom"
    anchor = build_anchor(settings)
    assert anchor is not None
    key = home / ".privyx" / "anchor.key"
    assert key.exists()
    assert await anchor.anchor("alice@example.com", Context()) == await HMACAnchor(
        key.read_text()
    ).anchor("alice@example.com", Context())
    assert settings.anchor.secret == "auto"


@pytest.mark.parametrize("secret", ["", "explicit-secret"])
async def test_configured_secret_overrides_run_default(
    config: Path, home: Path, secret: str
) -> None:
    await asyncio.to_thread(config.write_text, f"anchor:\n  secret: '{secret}'\n")
    settings = _run_settings(str(config), None, None, None, False)
    anchor = build_anchor(settings)
    if secret:
        assert anchor is not None
        assert await anchor.anchor("alice@example.com", Context()) == await HMACAnchor(
            secret
        ).anchor("alice@example.com", Context())
    else:
        assert anchor is None
    assert not (home / ".privyx" / "anchor.key").exists()


def test_mask_and_pipe_share_tokens_across_processes(config: Path, home: Path) -> None:
    outputs = []
    for command in (["mask", "alice@example.com"], ["pipe"]):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from privyx.cli.main import cli; cli()",
                "-c",
                str(config),
                *command,
            ],
            input="alice@example.com\n",
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())
    assert re.fullmatch(r"<PRIVYX_EMAIL_[0-9A-F]{16}>", outputs[0])
    assert outputs[0] == outputs[1]
    assert (home / ".privyx" / "anchor.key").exists()


@pytest.mark.parametrize(
    "options",
    [["config", "--show"], ["config", "--sources"], ["run", "--dry-run", "--no-anchor", "aider"]],
)
def test_inspection_does_not_provision_auto_secret(
    config: Path, home: Path, options: list[str]
) -> None:
    result = CliRunner().invoke(cli, ["-c", str(config), *options])
    assert result.exit_code == 0, result.output
    if options[0] == "run":
        assert "Anchor: auto-provision on launch" in result.output
    assert not (home / ".privyx" / "anchor.key").exists()


@pytest.mark.parametrize(
    "command", [["mask", "alice@example.com"], ["pipe"], ["proxy", "--port", "0"]]
)
def test_storage_failure_is_a_cli_error(config: Path, home: Path, command: list[str]) -> None:
    (home / ".privyx" / "anchor.key").mkdir(parents=True)
    result = CliRunner().invoke(cli, ["-c", str(config), *command])
    assert result.exit_code == 1
    assert "automatic anchor secret" in result.output
    assert "alice@example.com" not in result.output
    assert "Traceback" not in result.output


async def test_foreign_token_is_not_restored_as_another_sessions_value(config: Path) -> None:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    engine, close = await build_engine(load_config(config))
    try:
        first = await engine.transform("alice@example.com", session_id="first")
        second = await engine.transform("bob@example.com", session_id="second")
        assert first.text != second.text
        assert (await engine.restore(first.text, "second")).text == first.text
        assert (await engine.restore(first.text, "first")).text == "alice@example.com"
        assert (await engine.restore(second.text, "second")).text == "bob@example.com"
    finally:
        await close()


def test_environment_can_select_auto_or_override_it(
    config: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_ANCHOR_SECRET", "explicit-secret")
    explicit = CliRunner().invoke(cli, ["-c", str(config), "mask", "alice@example.com"])
    assert explicit.exit_code == 0, explicit.output
    assert not (home / ".privyx" / "anchor.key").exists()
    config.write_text("detector:\n  type: regex\n")
    monkeypatch.setenv("PRIVYX_ANCHOR_SECRET", "auto")
    automatic = CliRunner().invoke(cli, ["-c", str(config), "mask", "alice@example.com"])
    assert automatic.exit_code == 0, automatic.output
    assert (home / ".privyx" / "anchor.key").exists()
    assert automatic.stdout != explicit.stdout


def test_mcp_wrap_uses_the_same_anchor(config: Path) -> None:
    masked = CliRunner().invoke(cli, ["-c", str(config), "mask", "alice@example.com"])
    assert masked.exit_code == 0, masked.output
    server = (
        "import json, sys\n"
        "for line in sys.stdin:\n"
        " msg = json.loads(line)\n"
        " print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], "
        "'result': {'content': [{'type': 'text', 'text': 'alice@example.com'}]}}), flush=True)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from privyx.cli.main import cli; cli()",
            "-c",
            str(config),
            "mcp",
            "wrap",
            "--",
            sys.executable,
            "-c",
            server,
        ],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {}},
            }
        )
        + "\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    assert reply["result"]["content"][0]["text"] == masked.stdout.strip()
