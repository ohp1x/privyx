"""Secret references (``op://``, ``keychain://``) in credential settings."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from privyx import load_config
from privyx.cli.main import cli
from privyx.core import ConfigError
from privyx.secrets import _op, resolve_references

KEY = "sk-" + "ant-api03-abcdefghijklmnop"


@pytest.fixture
def op(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A fake `op` CLI that knows one reference."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "op://dev/anthropic/credential":
            return subprocess.CompletedProcess(argv, 0, KEY, "")
        return subprocess.CompletedProcess(argv, 1, "", "[ERROR] item not found\n")

    monkeypatch.setattr(_op.shutil, "which", lambda _: "/usr/bin/op")
    monkeypatch.setattr(_op.subprocess, "run", run)
    return calls


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("keyring")
    store = {("privyx", "anchor"): "anchor-secret-from-keychain"}
    module.get_password = lambda service, account: store.get((service, account))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)


def _yaml(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_op_reference_resolves_a_credential(tmp_path: Path, op: list[list[str]]) -> None:
    cfg = _yaml(tmp_path, "provider:\n  api_key: op://dev/anthropic/credential\n")
    assert load_config(cfg).provider.api_key == KEY
    assert op == [["op", "read", "--no-newline", "op://dev/anthropic/credential"]]


def test_header_and_env_values_resolve(tmp_path: Path, op: list[list[str]]) -> None:
    cfg = _yaml(
        tmp_path,
        "provider:\n  headers:\n    x-api-key: op://dev/anthropic/credential\n"
        "agents:\n  claude:\n    command: claude\n"
        "    env:\n      ANTHROPIC_API_KEY: op://dev/anthropic/credential\n",
    )
    assert load_config(cfg).provider.headers["x-api-key"] == KEY
    assert load_config(cfg, agent="claude").agents["claude"].env["ANTHROPIC_API_KEY"] == KEY


def test_another_profile_keeps_its_secret_references(tmp_path: Path, op: list[list[str]]) -> None:
    cfg = _yaml(
        tmp_path,
        "agents:\n  claude:\n    command: claude\n"
        "    env:\n      ANTHROPIC_API_KEY: op://dev/anthropic/credential\n"
        "    args: [op://dev/args/credential]\n"
        "    privyx:\n      anchor:\n        secret: keychain://privyx/anchor\n",
    )
    settings = load_config(cfg)
    assert op == []
    profile = settings.agents["claude"]
    assert profile.env["ANTHROPIC_API_KEY"] == "op://dev/anthropic/credential"
    assert profile.args == ["op://dev/args/credential"]
    assert load_config(cfg, agent="codex").agents["claude"].privyx["anchor"]["secret"] == (
        "keychain://privyx/anchor"
    )
    assert op == []


def test_only_an_applied_preset_resolves_its_secret_references(
    tmp_path: Path, op: list[list[str]]
) -> None:
    cfg = _yaml(
        tmp_path,
        "presets:\n  anchored:\n    anchor:\n      secret: op://dev/anthropic/credential\n",
    )
    settings = load_config(cfg, agent="claude")
    assert op == []
    assert settings.presets["anchored"]["anchor"]["secret"] == "op://dev/anthropic/credential"
    shown = CliRunner().invoke(cli, ["-c", str(cfg), "config", "--show"])
    assert "op://" not in shown.output
    assert load_config(cfg, agent="claude", presets=["anchored"]).anchor.secret == KEY
    assert len(op) == 1


def test_keychain_reference_resolves(tmp_path: Path, keychain: None) -> None:
    cfg = _yaml(tmp_path, "anchor:\n  secret: keychain://privyx/anchor\n")
    assert load_config(cfg).anchor.secret == "anchor-secret-from-keychain"


def test_missing_keychain_entry_fails(tmp_path: Path, keychain: None) -> None:
    cfg = _yaml(tmp_path, "anchor:\n  secret: keychain://privyx/other\n")
    with pytest.raises(ConfigError, match="no keychain entry"):
        load_config(cfg)


def test_keychain_without_the_extra_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # import fails
    cfg = _yaml(tmp_path, "anchor:\n  secret: keychain://privyx/anchor\n")
    with pytest.raises(ConfigError, match=r"privyx\[keychain\]"):
        load_config(cfg)


def test_a_failed_op_read_names_the_setting(tmp_path: Path, op: list[list[str]]) -> None:
    cfg = _yaml(tmp_path, "provider:\n  api_key: op://dev/missing/credential\n")
    with pytest.raises(ConfigError, match=r"provider\.api_key: \[ERROR\] item not found"):
        load_config(cfg)


def test_op_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_op.shutil, "which", lambda _: None)
    cfg = _yaml(tmp_path, "provider:\n  api_key: op://dev/anthropic/credential\n")
    with pytest.raises(ConfigError, match="`op` CLI is not installed"):
        load_config(cfg)


def test_a_reference_outside_a_credential_setting_fails(
    tmp_path: Path, op: list[list[str]]
) -> None:
    cfg = _yaml(tmp_path, "provider:\n  base_url: op://dev/anthropic/credential\n")
    with pytest.raises(ConfigError, match="provider.base_url holds a secret reference"):
        load_config(cfg)
    assert op == []  # never read


def test_a_resolved_secret_is_masked_in_config_show(
    tmp_path: Path, op: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIVYX_CONFIG", raising=False)
    cfg = _yaml(
        tmp_path,
        "provider:\n  api_key: op://dev/anthropic/credential\n"
        "  headers:\n    authorization: op://dev/anthropic/credential\n",
    )
    out = CliRunner().invoke(cli, ["-c", str(cfg), "config", "show"]).output
    assert KEY not in out
    assert json.loads(out)["provider"]["api_key"] == "***"


@pytest.mark.parametrize(
    "text",
    [
        "token:\n  namespace: op://dev/anthropic/credential\n",  # under a credential-like name
        "provider:\n  api_key:\n    nested: op://dev/anthropic/credential\n",
    ],
)
def test_a_reference_resolves_only_where_config_show_masks_it(
    tmp_path: Path, op: list[list[str]], text: str
) -> None:
    with pytest.raises(ConfigError, match="holds a secret reference"):
        load_config(_yaml(tmp_path, text))
    assert op == []


def test_every_resolved_value_is_masked_by_redact() -> None:
    from privyx.security import redact

    class Fixed:
        def resolve(self, reference: str, path: str) -> str:
            return KEY

    def resolver(scheme: str) -> Any:
        return Fixed() if scheme == "op" else None

    ref = "op://dev/anthropic/credential"
    data = {
        "provider": {"api_key": ref, "headers": {"authorization": ref}},
        "anchor": {"secret": ref},
        "tls": {"keyfile_password": ref},
        "agents": {"a.b": {"env": {"TOKEN": ref}}},
        "detector": [{"llm_api_key": ref}],
    }
    assert KEY not in str(redact(resolve_references(data, resolver)))
    assert KEY in str(resolve_references(data, resolver))


def test_redact_shows_the_auto_anchor_marker_but_masks_a_real_secret() -> None:
    from privyx.security import redact

    assert redact({"anchor": {"secret": "auto"}}) == {"anchor": {"secret": "auto"}}
    assert redact({"anchor": {"secret": "a" * 64}}) == {"anchor": {"secret": "***"}}
    assert redact({"client_secret": "auto"}) == {"client_secret": "***"}


# --- resolvers from plugins ---------------------------------------------------

VAULT_PLUGIN = """
from privyx.core import ConfigError
from privyx.secrets import SecretResolver


class AcmeVault(SecretResolver):
    name = "acmevault"

    class Options(SecretResolver.Options):
        project: str

    def resolve(self, reference, path):
        if reference != "acmevault://openai":
            raise ConfigError(f"{path}: not in {self.options.project}")
        return self.options.project + "-key"


def setup(app):
    app.add(AcmeVault)
"""


def _plugin(tmp_path: Path, text: str) -> Path:
    (tmp_path / "plugins").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "acmevault.py").write_text(VAULT_PLUGIN, encoding="utf-8")
    return _yaml(tmp_path, f"plugins:\n  paths: [{tmp_path / 'plugins'}]\n{text}")


def test_a_plugin_resolver_takes_its_scheme_and_its_options(tmp_path: Path) -> None:
    cfg = _plugin(
        tmp_path,
        "secrets:\n  acmevault:\n    project: apollo\nprovider:\n  api_key: acmevault://openai\n",
    )
    assert load_config(cfg).provider.api_key == "apollo-key"


def test_a_plugin_resolver_without_its_options_names_them(tmp_path: Path) -> None:
    cfg = _plugin(tmp_path, "provider:\n  api_key: acmevault://openai\n")
    with pytest.raises(ConfigError, match=r"'secrets\.acmevault\.project'"):
        load_config(cfg)


def test_a_reference_is_not_resolved_where_the_resolvers_come_from(tmp_path: Path) -> None:
    cfg = _plugin(tmp_path, "secrets:\n  acmevault:\n    project: acmevault://openai\n")
    with pytest.raises(ConfigError, match=r"holds a secret reference; write \$\{NAME\}"):
        load_config(cfg)


def test_an_unknown_scheme_is_a_value_and_an_unknown_resolver_an_error(tmp_path: Path) -> None:
    cfg = _yaml(tmp_path, "provider:\n  api_key: vault://openai\n")
    assert load_config(cfg).provider.api_key == "vault://openai"
    cfg = _yaml(tmp_path, "secrets:\n  vault: {}\n")
    with pytest.raises(ConfigError, match="unknown secret type 'vault'"):
        load_config(cfg)


def test_a_url_scheme_cannot_be_a_resolver() -> None:
    from privyx.secrets import SecretResolver

    with pytest.raises(TypeError, match="'https' is a URL scheme"):

        class Web(SecretResolver):
            name = "https"

            def resolve(self, reference: str, path: str) -> str:
                return ""
