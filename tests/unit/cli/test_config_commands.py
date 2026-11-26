"""`privyx config` get, set, unset, explain, validate, migrate, edit; `features`; `plugin`."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.config import is_trusted, trust


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRIVYX_PORT", raising=False)
    return CliRunner()


def _settings(home: Path) -> dict[str, Any]:
    return yaml.safe_load((home / ".privyx" / "settings.yaml").read_text(encoding="utf-8"))


def test_get_shows_a_value_and_set_changes_it_from_the_next_start(
    runner: CliRunner, home: Path
) -> None:
    assert runner.invoke(cli, ["config", "get", "server.port"]).output == "8000\n"

    changed = runner.invoke(cli, ["config", "set", "server.port", "8123"])

    assert changed.exit_code == 0, changed.output
    assert f"Set server.port in {home / '.privyx' / 'settings.yaml'}" in changed.output
    assert _settings(home) == {"server": {"port": 8123}}  # a number, as the setting is
    assert runner.invoke(cli, ["config", "get", "server.port"]).output == "8123\n"
    shown = runner.invoke(cli, ["config", "get", "detectors.regex", "--json"])
    assert json.loads(shown.output)["type"] == "regex"


def test_set_keeps_text_where_text_is_wanted_and_refuses_the_wrong_type(
    runner: CliRunner, home: Path
) -> None:
    assert runner.invoke(cli, ["config", "set", "token.namespace", "NO"]).exit_code == 0
    assert runner.invoke(cli, ["config", "set", "features.directive", "true"]).exit_code == 0
    wrong = runner.invoke(cli, ["config", "set", "server.port", "sk-" + "port-secret"])

    assert _settings(home) == {"token": {"namespace": "NO"}, "features": {"directive": True}}
    assert wrong.exit_code == 1
    assert "invalid value for 'server.port'" in wrong.output
    assert "port-secret" not in wrong.output


def test_set_refuses_a_key_that_is_no_setting(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(cli, ["config", "set", "server.prot", "1"])

    assert result.exit_code == 1
    assert "no setting 'server.prot' (did you mean 'server.port'?)" in result.output
    assert not (home / ".privyx" / "settings.yaml").exists()


def test_a_set_the_settings_refuse_leaves_the_file_as_it_was(runner: CliRunner, home: Path) -> None:
    assert runner.invoke(cli, ["config", "set", "server.port", "8123"]).exit_code == 0
    before = (home / ".privyx" / "settings.yaml").read_text(encoding="utf-8")

    result = runner.invoke(cli, ["config", "set", "server.port", "70000"])

    assert result.exit_code == 1
    assert (home / ".privyx" / "settings.yaml").read_text(encoding="utf-8") == before


def test_set_names_the_files_that_write_the_key_too(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = home / ".privyx" / "config.yaml"
    config.parent.mkdir()
    config.write_text("log:\n  level: info\nserver:\n  port: 9000\n", encoding="utf-8")

    below = runner.invoke(cli, ["config", "set", "server.port", "9001"])
    monkeypatch.setenv("PRIVYX_PORT", "9002")
    above = runner.invoke(cli, ["config", "set", "server.port", "9003"])

    assert f"{config}:4 also writes server.port; the value from here is used" in below.output
    assert "environment also writes server.port, and its value is used" in above.output


def test_unset_removes_the_key_and_what_it_leaves_empty(runner: CliRunner, home: Path) -> None:
    runner.invoke(cli, ["config", "set", "server.port", "8123"])
    runner.invoke(cli, ["config", "set", "log.level", "debug"])

    removed = runner.invoke(cli, ["config", "unset", "server.port"])
    missing = runner.invoke(cli, ["config", "unset", "server.port"])

    assert removed.exit_code == 0, removed.output
    assert _settings(home) == {"log": {"level": "debug"}}
    assert missing.exit_code == 1
    assert "does not set 'server.port'" in missing.output


def test_set_needs_a_file_with_a_named_config(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "named.yaml"
    config.write_text("{}\n", encoding="utf-8")
    written = tmp_path / "managed.yaml"

    refused = runner.invoke(cli, ["-c", str(config), "config", "set", "server.port", "1"])
    kept = runner.invoke(
        cli, ["-c", str(config), "config", "set", "server.port", "1", "--file", str(written)]
    )

    assert refused.exit_code == 1
    assert "name the file to write with --file" in refused.output
    assert kept.exit_code == 0, kept.output
    assert yaml.safe_load(written.read_text(encoding="utf-8")) == {"server": {"port": 1}}


def test_a_project_setting_is_trusted_and_a_file_changed_elsewhere_is_refused(
    runner: CliRunner, tmp_path: Path
) -> None:
    managed = tmp_path / ".privyx" / "settings.yaml"

    runner.invoke(cli, ["config", "set", "--project", "server.port", "8124"])
    assert is_trusted(managed)
    assert runner.invoke(cli, ["config", "get", "server.port"]).output == "8124\n"

    elsewhere = "server:\n  port: 8125\n"
    managed.write_text(elsewhere, encoding="utf-8")
    refused = runner.invoke(cli, ["config", "set", "--project", "log.level", "debug"])

    assert refused.exit_code == 1
    assert "not trusted" in refused.output
    assert managed.read_text(encoding="utf-8") == elsewhere and not is_trusted(managed)
    trust(managed)
    changed = runner.invoke(cli, ["config", "set", "--project", "log.level", "debug"])
    assert changed.exit_code == 0, changed.output
    assert is_trusted(managed)
    assert runner.invoke(cli, ["config", "get", "server.port"]).output == "8125\n"


def test_explain_names_the_value_its_layer_default_and_variables(
    runner: CliRunner, home: Path
) -> None:
    runner.invoke(cli, ["config", "set", "server.port", "8123"])

    shown = runner.invoke(cli, ["config", "explain", "server.port"])
    data = json.loads(runner.invoke(cli, ["config", "explain", "server.port", "--json"]).output)

    managed = str((home / ".privyx" / "settings.yaml").resolve())
    assert shown.output.splitlines() == [
        "server.port = 8123",
        f"  source:  {managed}",
        "  default: 8000",
        "  env:     PRIVYX_SERVER__PORT (also PRIVYX_PORT)",
        f"  written: {managed}:3",
    ]
    assert data["source"] == managed and data["default"] == 8000


def test_explain_masks_a_credential(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_API_KEY", "sk-" + "explained-secret")

    result = runner.invoke(cli, ["config", "explain", "provider.api_key"])

    assert result.exit_code == 0, result.output
    assert "explained-secret" not in result.output
    assert "provider.api_key = ***" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["get", "detectors.regex.terms.PERSON"],
        ["get", "detectors.regex.terms"],
        ["get", "detectors.regex"],
        ["get", "detectors.regex.patterns.CODE"],
        ["get", "agents.x.env.API_TOKEN"],
        ["explain", "detectors.regex.terms.PERSON"],
        ["explain", "detectors.regex.terms.PERSON", "--json"],
        ["explain", "agents.x.env.API_TOKEN"],
    ],
)
def test_get_and_explain_mask_the_values_inside_a_masked_field(
    runner: CliRunner, home: Path, command: list[str]
) -> None:
    key = "sk-" + "proj-abc123"
    (home / ".privyx").mkdir()
    (home / ".privyx" / "config.yaml").write_text(
        "detectors:\n  regex:\n    terms: {PERSON: [john smith]}\n"
        "    patterns: {CODE: 'acme-[0-9]+'}\n"
        f"agents:\n  x:\n    command: x\n    env: {{API_TOKEN: {key}}}\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli, ["config", *command])

    assert result.exit_code == 0, result.output
    assert "***" in result.output
    assert not [value for value in ("john", "acme", key) if value in result.output]


def test_the_managed_file_is_owner_only(runner: CliRunner, home: Path) -> None:
    assert runner.invoke(cli, ["config", "set", "server.port", "8123"]).exit_code == 0

    assert stat.S_IMODE((home / ".privyx" / "settings.yaml").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "command",
    [
        ["config", "set", "server.port", "8123"],  # settings.yaml
        ["config", "trust"],  # trusted.json
        ["mask", "mail ana@example.com"],  # anchor.key
        ["session", "list"],  # privyx.db
    ],
)
def test_the_privyx_directory_is_owner_only(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    # settings.yaml can load plugins, and trusted.json picks the configs that may.
    Path("privyx.yaml").write_text("server:\n  port: 9001\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_VAULT", "sqlite")
    umask = os.umask(0o002)  # mkdir() alone would then give it 0775
    try:
        result = runner.invoke(cli, command)
    finally:
        os.umask(umask)

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE((home / ".privyx").stat().st_mode) == 0o700


def test_set_with_file_backs_up_a_file_written_by_hand(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "mine.yaml"
    text = "# my port\nserver:\n  port: 8001\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o640)
    named = ["-c", str(path), "config", "set", "--file", str(path)]

    first = runner.invoke(cli, [*named, "server.port", "8002"])
    second = runner.invoke(cli, [*named, "log.level", "debug"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "without its comments" in first.output
    assert "without its comments" not in second.output  # it is Privyx's to write by now
    assert (tmp_path / "mine.yaml.bak").read_text(encoding="utf-8") == text
    assert not (tmp_path / "mine.yaml.bak.1").exists()
    written = path.read_text(encoding="utf-8")
    assert yaml.safe_load(written) == {"server": {"port": 8002}, "log": {"level": "debug"}}
    assert "wins over" not in written  # true of the managed file only
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_refused_set_leaves_a_file_by_hand_as_it_was(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "mine.yaml"
    text = "# my port\nserver:\n  port: 8001\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o640)

    refused = runner.invoke(cli, ["config", "set", "--file", str(path), "server.port", "70000"])

    assert refused.exit_code == 1
    assert path.read_text(encoding="utf-8") == text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(tmp_path.glob("mine.yaml.*")) == []


@pytest.mark.parametrize("link", [".privyx/settings.yaml", ".privyx"])
def test_a_link_in_the_project_is_not_written_through(
    runner: CliRunner, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, link: str
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "settings.yaml").write_text("log:\n  level: info\n", encoding="utf-8")
    if link == ".privyx":
        (tmp_path / link).symlink_to(outside)
    else:
        (tmp_path / ".privyx").mkdir()
        (tmp_path / link).symlink_to(outside / "settings.yaml")

    result = runner.invoke(cli, ["config", "set", "--project", "server.port", "8124"])

    assert result.exit_code == 1
    assert "symbolic link" in result.output
    assert (outside / "settings.yaml").read_text(encoding="utf-8") == "log:\n  level: info\n"


def test_set_with_file_does_not_run_the_plugins_it_names(runner: CliRunner, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    (tmp_path / "plugin.py").write_text(
        f"from pathlib import Path\nPath({str(ran)!r}).touch()\ndef setup(app):\n    pass\n",
        encoding="utf-8",
    )
    path = tmp_path / "other.yaml"
    path.write_text("plugins:\n  enabled: true\n  paths: [plugin.py]\n", encoding="utf-8")

    result = runner.invoke(cli, ["config", "set", "--file", str(path), "server.port", "8002"])

    assert result.exit_code == 0, result.output
    assert not ran.exists()


def test_set_keeps_the_provider_a_file_picks_by_name(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "mine.yaml"
    path.write_text("provider: openai\n", encoding="utf-8")
    named = ["-c", str(path), "config", "set", "--file", str(path)]

    result = runner.invoke(cli, [*named, "provider.timeout", "12"])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "provider": "openai",
        "providers": {"openai": {"timeout": 12.0}},
    }


def test_set_under_an_entry_switched_off_keeps_it_off(runner: CliRunner, home: Path) -> None:
    runner.invoke(cli, ["config", "set", "features.audit", "false"])

    result = runner.invoke(cli, ["config", "set", "features.audit.path", "audit.log"])

    assert result.exit_code == 0, result.output
    assert _settings(home)["features"]["audit"] == {"enabled": False, "path": "audit.log"}


def test_set_needs_a_file_when_discovery_is_off(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_NO_DISCOVERY", "1")

    result = runner.invoke(cli, ["config", "set", "server.port", "8123"])

    assert result.exit_code == 1
    assert "--file" in result.output
    assert not (home / ".privyx" / "settings.yaml").exists()


def test_a_project_file_that_includes_the_managed_one_stays_trusted(
    runner: CliRunner, tmp_path: Path
) -> None:
    managed = tmp_path / ".privyx" / "settings.yaml"
    managed.parent.mkdir()
    managed.write_text("log:\n  level: info\n", encoding="utf-8")
    top = tmp_path / "privyx.yaml"
    top.write_text("include: .privyx/settings.yaml\n", encoding="utf-8")
    assert runner.invoke(cli, ["config", "trust"]).exit_code == 0

    result = runner.invoke(cli, ["config", "set", "--project", "server.port", "8123"])

    assert result.exit_code == 0, result.output
    assert is_trusted(top) and is_trusted(managed)


def test_migrate_write_keeps_each_backup_with_the_mode_of_the_file(
    runner: CliRunner, tmp_path: Path
) -> None:
    path = tmp_path / "mine.yaml"
    path.write_text("port: 8001\n", encoding="utf-8")
    path.chmod(0o600)
    (tmp_path / "mine.yaml.bak").write_text("earlier\n", encoding="utf-8")

    result = runner.invoke(cli, ["config", "migrate", str(path), "--write"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "mine.yaml.bak").read_text(encoding="utf-8") == "earlier\n"
    backup = tmp_path / "mine.yaml.bak.1"
    assert backup.read_text(encoding="utf-8") == "port: 8001\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_validate_names_the_old_keys_and_fails_on_an_invalid_config(
    runner: CliRunner, tmp_path: Path
) -> None:
    old = tmp_path / "old.yaml"
    old.write_text("port: 8001\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("server:\n  port: 70000\n", encoding="utf-8")

    checked = runner.invoke(cli, ["config", "validate", str(old)])
    failed = runner.invoke(cli, ["config", "validate", str(bad)])

    assert checked.exit_code == 0
    assert checked.stdout == "valid\n"
    assert "'port' is deprecated; write 'server.port'" in checked.stderr
    assert failed.exit_code == 1


def test_migrate_prints_the_new_config_and_write_keeps_a_trusted_backup(
    runner: CliRunner, tmp_path: Path
) -> None:
    project = tmp_path / "privyx.yaml"
    project.write_text("# mine\nport: 8001\ndetector:\n  terms: {P: [ann]}\n", encoding="utf-8")
    trust(project)

    printed = runner.invoke(cli, ["config", "migrate", str(project)])
    written = runner.invoke(cli, ["config", "migrate", str(project), "--write"])

    assert yaml.safe_load(printed.stdout) == {
        "server": {"port": 8001},
        "detectors": {"regex": {"terms": {"P": ["ann"]}}},
    }
    assert "Comments are not kept" in printed.stderr
    assert written.exit_code == 0, written.output
    assert (tmp_path / "privyx.yaml.bak").read_text(encoding="utf-8").startswith("# mine")
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["server"] == {"port": 8001}
    assert is_trusted(project)
    again = runner.invoke(cli, ["config", "migrate", str(project)])
    assert "nothing to migrate" in again.stderr


def test_migrate_prints_a_file_with_nothing_to_rename_as_it_is(
    runner: CliRunner, home: Path, tmp_path: Path
) -> None:
    user = home / ".privyx" / "config.yaml"
    user.parent.mkdir()
    user.write_text("# mine\ndetectors:\n  regex:\n    terms: {P: [ann]}", encoding="utf-8")
    project = tmp_path / "privyx.yaml"
    project.write_text("port: 8001\n", encoding="utf-8")
    trust(project)

    one = runner.invoke(cli, ["config", "migrate", str(user)])
    each = runner.invoke(cli, ["config", "migrate"])

    # `config migrate FILE > new.yaml` must not write an empty config, losing the terms.
    assert one.exit_code == 0, one.output
    assert one.stdout == user.read_text(encoding="utf-8")
    assert "nothing to migrate" in one.stderr
    assert each.exit_code == 0, each.output
    assert f"terms: {{P: [ann]}}\n# {project}\nserver:\n  port: 8001\n" in each.stdout


def test_routes_shows_the_masked_paths(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["config", "routes"])

    assert result.exit_code == 0, result.output
    assert ["/v1/messages", "anthropic"] in [line.split() for line in result.output.splitlines()]
    assert "Other paths: forwarded unchanged, without masking" in result.output


_RELAY = """
from privyx.dialects import Dialect
from privyx.providers import GoogleProvider


class Relay(GoogleProvider):
    name = "acme-relay"


class Converse(Dialect):
    name = "acme-converse"
    content_keys = frozenset({"messages"})
    default_routes = ("/model/*/converse",)


def setup(app):
    app.add(Relay)
    app.add(Converse)
"""


def _relay_config(tmp_path: Path) -> Path:
    plugin = tmp_path / "relay.py"
    plugin.write_text(_RELAY, encoding="utf-8")
    config = tmp_path / "config.yaml"
    written = {
        "plugins": {"paths": [str(plugin)]},
        "provider": {"type": "acme-relay", "base_url": "https://llm.example.com"},
        "proxy": {"mode": "gateway"},
    }
    config.write_text(yaml.safe_dump(written), encoding="utf-8")
    return config


def test_routes_shows_what_the_proxy_serves(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["-c", str(_relay_config(tmp_path)), "config", "routes"])

    assert result.exit_code == 0, result.output
    shown = [line.split() for line in result.output.splitlines()]
    assert ["/*/models/*:generateContent", "gemini"] in shown  # the provider extends google's
    assert ["/model/*/converse", "acme-converse"] in shown  # the plugin's dialect brought it


def test_doctor_counts_what_the_proxy_serves(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["-c", str(_relay_config(tmp_path)), "doctor", "--json"])

    routes = next(c for c in json.loads(result.stdout) if c["name"] == "routes")
    assert routes["ok"], routes
    assert "/*/models/*:generateContent (gemini)" in routes["message"]
    assert "/model/*/converse (acme-converse)" in routes["message"]


def test_edit_opens_the_config_file_and_checks_it(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    def edit(filename: str) -> None:
        opened.append(filename)
        Path(filename).write_text("port: 8001\n", encoding="utf-8")

    monkeypatch.setattr("click.edit", edit)

    result = runner.invoke(cli, ["config", "edit"])

    assert opened == [str(home / ".privyx" / "config.yaml")]
    assert result.exit_code == 0, result.output
    assert "'port' is deprecated" in result.stderr


def test_config_changed_is_heard_without_the_value(home: Path) -> None:
    from privyx import Privyx
    from privyx.events import ConfigChanged

    app = Privyx()
    heard: list[ConfigChanged] = []
    app.on(ConfigChanged)(heard.append)

    app.config.set("features.directive", True)
    app.config.unset("features.directive")

    managed = str(home / ".privyx" / "settings.yaml")
    assert [(e.key, e.file, e.action) for e in heard] == [
        ("features.directive", managed, "set"),
        ("features.directive", managed, "unset"),
    ]


def test_features_enable_and_disable_write_the_managed_settings(
    runner: CliRunner, home: Path
) -> None:
    enabled = runner.invoke(cli, ["features", "enable", "directive"])
    listed = json.loads(runner.invoke(cli, ["features", "list", "--json"]).output)
    disabled = runner.invoke(cli, ["features", "disable", "audit"])
    unknown = runner.invoke(cli, ["features", "enable", "directiv"])

    assert enabled.exit_code == 0, enabled.output
    assert {f["name"]: f["enabled"] for f in listed}["directive"] is True
    assert disabled.exit_code == 0, disabled.output
    assert _settings(home) == {
        "features": {"directive": {"enabled": True}, "audit": {"enabled": False}}
    }
    assert ["audit", "off"] in [
        line.split() for line in runner.invoke(cli, ["features", "list"]).output.splitlines()
    ]
    assert unknown.exit_code == 1
    assert "no feature 'directiv' (did you mean 'directive'?)" in unknown.output


PLUGIN = """
from privyx.core import Detection
from privyx.detectors import Detector


class Plates(Detector):
    name = "plates"

    def detect_sync(self, text, context):
        return Detection()


def setup(app):
    app.add(Plates)
"""


def test_plugin_list_names_what_each_plugin_adds(runner: CliRunner, tmp_path: Path) -> None:
    plugin = tmp_path / "plates.py"
    plugin.write_text(PLUGIN, encoding="utf-8")
    config = tmp_path / "c.yaml"
    config.write_text(f"plugins:\n  paths: [{plugin}]\n", encoding="utf-8")

    shown = runner.invoke(cli, ["-c", str(config), "plugin", "list"])
    data = json.loads(runner.invoke(cli, ["-c", str(config), "plugin", "list", "--json"]).output)

    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines()[:2] == [
        f"plugins: {plugin} (plugins.paths)",
        f"  detector  plates          {plugin}",
    ]
    assert data["loaded"] == [["plugins.paths", str(plugin)]]
    assert data["components"] == [
        {"kind": "detector", "name": "plates", "source": str(plugin), "extends": None}
    ]


def test_doctor_writes_its_checks_as_json(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["doctor", "--json"])

    checks = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert {"name", "ok", "message"} == set(checks[0])
    assert [c["name"] for c in checks if c["name"] == "configuration"] == ["configuration"]


def test_run_writes_the_plan_and_the_list_as_json(runner: CliRunner) -> None:
    plan = runner.invoke(cli, ["run", "--dry-run", "--json", "claude"])
    targets = runner.invoke(cli, ["run", "--list", "--json"])
    alone = runner.invoke(cli, ["run", "--json", "claude"])

    data = json.loads(plan.stdout)
    assert (data["profile"], data["type"], data["command"]) == ("claude", "claude", "claude")
    assert data["provider"] == {"name": "anthropic", "source": "defaults"}
    assert data["environment"]["ANTHROPIC_BASE_URL"]["value"] == data["proxy"]["url"]
    assert {"name": "claude", "type": "claude", "provider": "anthropic"}.items() <= next(
        t for t in json.loads(targets.stdout) if t["name"] == "claude"
    ).items()
    assert alone.exit_code == 2


def test_session_list_and_audit_stats_write_json(runner: CliRunner, tmp_path: Path) -> None:
    log = tmp_path / "audit.log"
    log.write_text(
        json.dumps({"schema_version": 1, "ts": 1.0, "event": "proxy.request"}) + "\n",
        encoding="utf-8",
    )

    sessions = json.loads(runner.invoke(cli, ["session", "list", "--json"]).stdout)
    stats = json.loads(runner.invoke(cli, ["audit", "stats", str(log), "--json"]).stdout)

    assert sessions == {"vault": "memory", "sessions": []}
    assert stats["events"] == {"proxy.request": 1}


def test_hook_install_and_its_old_name(runner: CliRunner, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    installed = runner.invoke(cli, ["hook", "install"])
    again = runner.invoke(cli, ["install-hook"])

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert installed.exit_code == 0, installed.output
    assert "# Installed by `privyx hook install`." in hook.read_text(encoding="utf-8")
    assert again.exit_code == 0, again.output
    assert "`privyx install-hook` is deprecated; use `privyx hook install`" in again.stderr


def test_a_hook_of_an_earlier_version_is_replaced(runner: CliRunner, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n# Installed by `privyx install-hook`.\nexec privyx hook pre-commit\n"
    )

    result = runner.invoke(cli, ["hook", "install"])

    assert result.exit_code == 0, result.output
    assert "# Installed by `privyx hook install`." in hook.read_text(encoding="utf-8")
