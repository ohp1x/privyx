"""Tests for the CLI surface.

The CLI is orchestration only (principle #12), so these tests check that each
command reaches the *configured* components — the failure mode being a command
that quietly hardcodes its own detector or vault and ignores ``--config``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.main import cli

EMAIL = "alice@example.com"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sqlite_config(tmp_path: Path) -> Path:
    """A config with a real on-disk vault, so state survives between commands."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "vault:\n"
        "  type: sqlite\n"
        f"  dsn: sqlite+aiosqlite:///{tmp_path / 'privyx.db'}\n"
        "detector:\n"
        "  type: regex\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# detect


def test_detect_reports_spans(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["detect", f"mail {EMAIL}"])
    assert result.exit_code == 0
    assert "EMAIL" in result.output
    assert EMAIL in result.output


def test_detect_transform_shows_pseudonymized_text(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["detect", "--transform", f"mail {EMAIL}"])
    assert result.exit_code == 0
    assert "<PRIVYX_EMAIL_1>" in result.output


def test_detect_honours_config_patterns(runner: CliRunner, tmp_path: Path) -> None:
    """A custom pattern from the config file is actually used."""
    config = tmp_path / "c.yaml"
    config.write_text(
        'detector:\n  type: regex\n  patterns:\n    TICKET: "TCK-[0-9]{4}"\n', encoding="utf-8"
    )

    result = runner.invoke(cli, ["detect", "-c", str(config), "see TCK-1234"])

    assert result.exit_code == 0
    assert "TICKET" in result.output


def test_detect_applies_policy(runner: CliRunner, tmp_path: Path) -> None:
    """``--no-policy`` shows raw detections; the default filters them."""
    config = tmp_path / "c.yaml"
    config.write_text("policy:\n  type: strict\n  allowed: [SSN]\n", encoding="utf-8")

    filtered = runner.invoke(cli, ["detect", "-c", str(config), f"mail {EMAIL}"])
    unfiltered = runner.invoke(cli, ["detect", "-c", str(config), "--no-policy", f"mail {EMAIL}"])

    assert "No sensitive entities detected." in filtered.output
    assert "EMAIL" in unfiltered.output


def test_detect_without_text_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["detect"])
    assert result.exit_code == 1


def test_detect_reports_bad_config(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text('detector:\n  patterns:\n    BAD: "([unclosed"\n', encoding="utf-8")

    result = runner.invoke(cli, ["detect", "-c", str(config), "text"])

    assert result.exit_code == 1
    assert "Error:" in result.output


# --------------------------------------------------------------------------
# inspect session


def test_inspect_session_masks_values_by_default(runner: CliRunner, sqlite_config: Path) -> None:
    """A session written by one command is readable by the next — and masked.

    Masking is principle #11: routine inspection must not print PII.
    """
    session_id = _seed_session(sqlite_config)

    result = runner.invoke(cli, ["inspect", "session", "-c", str(sqlite_config), session_id])

    assert result.exit_code == 0
    assert "<PRIVYX_EMAIL_1>" in result.output
    assert EMAIL not in result.output
    assert "al" in result.output and "*" in result.output
    assert "--reveal" in result.output


def test_inspect_session_reveal_shows_values(runner: CliRunner, sqlite_config: Path) -> None:
    session_id = _seed_session(sqlite_config)

    result = runner.invoke(
        cli, ["inspect", "session", "-c", str(sqlite_config), "--reveal", session_id]
    )

    assert result.exit_code == 0
    assert EMAIL in result.output


def test_inspect_session_missing_exits_nonzero(runner: CliRunner, sqlite_config: Path) -> None:
    result = runner.invoke(cli, ["inspect", "session", "-c", str(sqlite_config), "ses_nope"])

    assert result.exit_code == 1
    assert "Session not found" in result.output


# session list / prune (show is the same command as `inspect session`)


def test_session_list_shows_counts_not_values(runner: CliRunner, sqlite_config: Path) -> None:
    session_id = _seed_session(sqlite_config)

    result = runner.invoke(cli, ["session", "list", "-c", str(sqlite_config)])

    assert result.exit_code == 0
    assert session_id in result.output
    assert "1 session(s)" in result.output
    assert EMAIL not in result.output


def test_session_prune_deletes_idle_sessions_and_audits(
    runner: CliRunner, sqlite_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_log = tmp_path / "audit.log"
    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(audit_log))
    idle, active = _seed_session(sqlite_config), _seed_session(sqlite_config)
    _age_session(sqlite_config, idle, 8 * 86400)
    prune = ["session", "prune", "-c", str(sqlite_config), "--older-than", "7d"]

    dry = runner.invoke(cli, [*prune, "--dry-run"])
    assert dry.exit_code == 0
    assert idle in dry.output and "Would prune 1 session(s)" in dry.output
    assert not audit_log.exists()

    result = runner.invoke(cli, prune)
    assert result.exit_code == 0
    assert "Pruned 1 session(s)" in result.output

    listed = runner.invoke(cli, ["session", "list", "-c", str(sqlite_config)]).output
    assert idle not in listed and active in listed
    events = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert [(e["event"], e["session_id"], e["reason"], e["mapping_count"]) for e in events] == [
        ("session.deleted", idle, "prune", 1)
    ]


def _write_audit_log(path: Path) -> None:
    now = time.time()
    records = [
        {"ts": now - 10 * 86400, "event": "session.created", "session_id": "ses_old"},
        {"ts": now, "event": "session.created", "request_id": "req_1", "session_id": "ses_1"},
        {
            "ts": now,
            "event": "session.transform",
            "request_id": "req_1",
            "session_id": "ses_1",
            "entity_counts": {"EMAIL": 2, "PERSON": 1},
            "transformations": 3,
        },
        {"ts": now, "event": "proxy.request", "request_id": "req_1", "status": 200},
        {"ts": now, "event": "session.restore", "request_id": "req_1", "transformations": 2},
        {"ts": now, "event": "proxy.response", "request_id": "req_1", "duration_ms": 1500.0},
        {
            "ts": now,
            "event": "proxy.error",
            "request_id": "req_2",
            "phase": "upstream",
            "error_type": "ConnectError",
        },
    ]
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join([*lines, "not json"]) + "\n", encoding="utf-8")


def test_audit_stats_summarizes_the_log(runner: CliRunner, tmp_path: Path) -> None:
    log = tmp_path / "audit.log"
    _write_audit_log(log)

    result = runner.invoke(cli, ["audit", "stats", str(log)])

    assert result.exit_code == 0
    assert "Requests:   1" in result.output
    assert "Responses:  1 (avg 1.50s)" in result.output
    assert "Errors:     1 (upstream 1)" in result.output
    assert "Sessions:   2 created, 0 deleted" in result.output
    assert "Masked:     3 entities" in result.output
    assert "Restored:   2 pseudonyms" in result.output
    assert "EMAIL   2" in result.output
    assert "Skipped 1 unreadable line(s)" in result.output

    recent = runner.invoke(cli, ["audit", "stats", str(log), "--since", "7d"])
    assert "Sessions:   1 created" in recent.output


def test_audit_stats_reads_the_configured_path(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "audit.log"
    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(log))

    missing = runner.invoke(cli, ["audit", "stats"])
    assert missing.exit_code == 1
    assert "No such file" in missing.output

    _write_audit_log(log)
    assert "Requests:   1" in runner.invoke(cli, ["audit", "stats"]).output


def test_audit_tail_prints_the_last_events_readably(runner: CliRunner, tmp_path: Path) -> None:
    log = tmp_path / "audit.log"
    _write_audit_log(log)

    result = runner.invoke(cli, ["audit", "tail", str(log), "-n", "3", "--no-follow"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 3
    assert "proxy.response" in lines[0] and "req_1" in lines[0] and "duration_ms=1500.0" in lines[0]
    assert "phase=upstream error_type=ConnectError" in lines[1]
    assert lines[2] == "not json"


def test_session_prune_rejects_a_bare_number(runner: CliRunner, sqlite_config: Path) -> None:
    result = runner.invoke(cli, ["session", "prune", "-c", str(sqlite_config), "--older-than", "7"])

    assert result.exit_code == 2
    assert "e.g. 7d" in result.output


def _age_session(config: Path, session_id: str, seconds: float) -> None:
    """Push a session's last activity ``seconds`` into the past."""
    import asyncio

    from privyx.config.loader import load_config
    from privyx.core.builder import build_vault

    async def _run() -> None:
        vault, close = await build_vault(load_config(config))
        try:
            session = await vault.get(session_id)
            assert session is not None
            session.updated_at -= seconds
            await vault.save(session)
        finally:
            await close()

    asyncio.run(_run())


def _seed_session(config: Path) -> str:
    """Write one session through the configured vault and return its id."""
    import asyncio

    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    async def _run() -> str:
        engine, close = await build_engine(load_config(config))
        try:
            session = await engine.get_or_create_session()
            await engine.transform(f"mail {EMAIL}", session=session)
            return session.session_id
        finally:
            await close()

    return asyncio.run(_run())


# --------------------------------------------------------------------------
# doctor


def test_doctor_all_checks_pass_with_defaults(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0
    checks = ("plugins", "detector", "vault", "provider", "proxy", "streaming", "configuration")
    for check in checks:
        assert f"✓ {check}" in result.output


def test_doctor_covers_every_documented_check(runner: CliRunner) -> None:
    """context.md lists six checks, plus the plugins check the plugin system adds."""
    result = runner.invoke(cli, ["doctor"])
    assert result.output.count("✓") + result.output.count("✗") == 7


def test_doctor_fails_on_broken_config(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text('detector:\n  patterns:\n    BAD: "([unclosed"\n', encoding="utf-8")

    result = runner.invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 1
    assert "✗ detector" in result.output


def test_doctor_uses_the_configured_vault(runner: CliRunner, sqlite_config: Path) -> None:
    result = runner.invoke(cli, ["doctor", "-c", str(sqlite_config)])

    assert result.exit_code == 0
    assert "✓ vault: sqlite" in result.output


# --------------------------------------------------------------------------
# run


def test_run_lists_known_targets(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "--list"])

    assert result.exit_code == 0
    assert "claude" in result.output
    assert "ANTHROPIC_BASE_URL" in result.output


def test_run_without_target_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run"])
    assert result.exit_code != 0
    assert "TARGET" in result.output


def test_run_unknown_command_exits_127(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "definitely-not-installed", "--env-var", "SOME_BASE_URL"])

    assert result.exit_code == 127
    assert "not found on PATH" in result.output


def test_run_unknown_target_without_env_var_is_a_usage_error(runner: CliRunner) -> None:
    """An unrecognised tool needs --env-var; guessing would silently do nothing."""
    result = runner.invoke(cli, ["run", "some-tool"])

    assert result.exit_code != 0
    assert "--env-var" in result.output


async def test_run_spawns_target_against_a_live_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawned process really can reach the proxy through the injected URL.

    This is the whole point of ``privyx run``: an unmodified tool talks to what
    it thinks is the provider.  The child asserts on its own side and exits
    non-zero if the URL is unreachable, so a proxy that never started — or
    bound to a different port — fails here rather than at first use.
    """
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    # `run` writes its audit trail to audit.path; keep it inside tmp_path so the
    # test does not drop a privyx-audit.log in the repo root.
    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))

    from privyx.cli.commands.run import Target, _run_target

    result_file = tmp_path / "result.json"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, pathlib, urllib.request\n"
        "url = os.environ['PROBE_BASE_URL']\n"
        "with urllib.request.urlopen(url + '/health', timeout=5) as fh:\n"
        "    health = json.load(fh)\n"
        f"out = pathlib.Path({str(result_file)!r})\n"
        "out.write_text(json.dumps({'url': url, 'health': health}))\n",
        encoding="utf-8",
    )

    spec = Target(command=sys.executable, provider="generic", env_vars=("PROBE_BASE_URL",))
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=None,
        upstream=None,
        port=0,
        env_vars=("PROBE_BASE_URL",),
        # Keep the test hermetic: no per-user anchor.key written under $HOME.
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0, "the spawned tool could not reach the proxy"
    recorded = json.loads(result_file.read_text())
    assert recorded["health"] == {"status": "ok"}
    assert recorded["url"].startswith("http://127.0.0.1:")


async def test_run_provisions_anchor_secret_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By default `privyx run` persists an anchor secret, so aliases stay stable."""
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PRIVYX_ANCHOR_SECRET", raising=False)

    from privyx.cli.commands.run import Target, _run_target

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.request\n"
        "urllib.request.urlopen(os.environ['PROBE_BASE_URL'] + '/health', timeout=5).read()\n",
        encoding="utf-8",
    )
    spec = Target(command=sys.executable, provider="generic", env_vars=("PROBE_BASE_URL",))

    # Defaults: session=conversation, auto-anchor on.
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=None,
        upstream=None,
        port=0,
        env_vars=("PROBE_BASE_URL",),
    )

    assert code == 0
    anchor_key = tmp_path / "config" / "privyx" / "anchor.key"
    assert anchor_key.exists()
    secret = anchor_key.read_text(encoding="utf-8").strip()
    assert len(secret) == 64
    int(secret, 16)  # valid hex


# --------------------------------------------------------------------------
# config


def test_config_show_outputs_settings(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["config", "--show"])

    assert result.exit_code == 0
    assert "detector" in result.output or "vault" in result.output


@pytest.mark.parametrize("args", [["config"], ["config", "--show"]])
def test_config_reports_bad_config_without_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "bogus")

    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    assert "Error: invalid configuration" in result.output
    assert "Traceback" not in result.output


def test_config_show_masks_secrets(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The last two come from detector config, which spells out what it hides.
    secrets = ["sk-env-key", "hunter2", "anchor-secret", "Bearer hdr-token", "llm-key"]
    secrets += ["Ann Acme", "Falcon"]
    config = tmp_path / "c.yaml"
    config.write_text(
        "vault:\n"
        "  redis_url: redis://:hunter2@cache:6379/0\n"
        "anchor:\n  secret: anchor-secret\n"
        "provider:\n  headers:\n    Authorization: Bearer hdr-token\n"
        "detector:\n"
        "  - type: regex\n"
        "    patterns: {CODENAME: 'Project\\s+Falcon'}\n"
        "    terms: {PERSON: [Ann Acme]}\n"
        "  - type: llm\n"
        "    llm_api_key: llm-key\n"
        "    llm_instructions: Also mask Project Falcon\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVYX_OPENAI_API_KEY", "sk-env-key")
    monkeypatch.delenv("PRIVYX_API_KEY", raising=False)

    result = runner.invoke(cli, ["config", "--show", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert not [s for s in secrets if s in result.output]
    shown = json.loads(result.output)
    assert shown["vault"]["redis_url"] == "redis://:***@cache:6379/0"
    assert shown["provider"]["openai_api_key"] == "***"
    assert shown["provider"]["api_key"] == ""  # unset stays visible as unset
    assert shown["detector"][0]["patterns"] == {"CODENAME": "***"}  # names stay
    assert shown["detector"][0]["terms"] == {"PERSON": ["***"]}


def test_cli_exposes_every_documented_command(runner: CliRunner) -> None:
    """The CLI tree: proxy, run, detect, mask, unmask, inspect, session, audit, config, doctor."""
    result = runner.invoke(cli, ["--help"])

    for command in (
        "proxy",
        "run",
        "detect",
        "mask",
        "unmask",
        "inspect",
        "session",
        "audit",
        "config",
        "doctor",
    ):
        assert command in result.output

    sub = runner.invoke(cli, ["inspect", "--help"])
    assert "session" in sub.output
    sub = runner.invoke(cli, ["session", "--help"])
    for command in ("list", "show", "prune"):
        assert command in sub.output
    sub = runner.invoke(cli, ["audit", "--help"])
    for command in ("stats", "tail"):
        assert command in sub.output


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "privyx" in result.output


def test_detect_stdin(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["detect", "--stdin"], input=f"mail {EMAIL}\n")

    assert result.exit_code == 0
    assert "EMAIL" in result.output


def test_detect_json_safe_output_has_no_raw_pii_when_masked() -> None:
    """Sanity-check the masking helper itself."""
    from privyx.cli.commands.session import _mask

    assert _mask("alice@example.com") == "al*************om"
    assert _mask("abc") == "***"
    assert json.dumps(_mask(EMAIL))  # serializable, no surprises


# --------------------------------------------------------------------------
# mask / unmask


def test_mask_unmask_round_trips_through_a_map_file(runner: CliRunner, tmp_path: Path) -> None:
    map_path = tmp_path / "m.json"

    masked = runner.invoke(cli, ["mask", "--map", str(map_path), f"mail {EMAIL}"])
    assert masked.exit_code == 0
    assert "<PRIVYX_EMAIL_1>" in masked.stdout
    assert EMAIL not in masked.stdout

    back = runner.invoke(cli, ["unmask", "--map", str(map_path), masked.stdout.strip()])
    assert back.exit_code == 0
    assert back.stdout.strip() == f"mail {EMAIL}"


def test_mask_continues_an_existing_map_instead_of_overwriting_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A second document masked against the same map keeps the first one's tokens."""
    map_path = tmp_path / "m.json"

    first = runner.invoke(cli, ["mask", "--map", str(map_path), EMAIL])
    second = runner.invoke(cli, ["mask", "--map", str(map_path), f"bob@example.com and {EMAIL}"])

    assert first.stdout.strip() in second.stdout
    mapping = json.loads(map_path.read_text(encoding="utf-8"))["mapping"]
    assert sorted(mapping.values()) == [EMAIL, "bob@example.com"]


def test_mask_unmask_round_trips_through_the_vault(runner: CliRunner, sqlite_config: Path) -> None:
    masked = runner.invoke(
        cli, ["mask", "-c", str(sqlite_config), "--session", "demo", f"mail {EMAIL}"]
    )
    assert masked.exit_code == 0
    assert "session: demo" in masked.stderr

    back = runner.invoke(
        cli, ["unmask", "-c", str(sqlite_config), "--session", "demo", masked.stdout.strip()]
    )
    assert back.exit_code == 0
    assert back.stdout.strip() == f"mail {EMAIL}"


def test_mask_json_keeps_structure_and_non_string_values(runner: CliRunner, tmp_path: Path) -> None:
    """Also covers format auto-detection: the .json extension picks the JSON walk."""
    source = tmp_path / "in.json"
    source.write_text(
        json.dumps({"user": {"email": EMAIL, "age": 30, "ok": True, "x": None}}), encoding="utf-8"
    )

    result = runner.invoke(cli, ["mask", "--map", str(tmp_path / "m.json"), "-i", str(source)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "user": {"email": "<PRIVYX_EMAIL_1>", "age": 30, "ok": True, "x": None}
    }


def test_mask_json_path_limits_the_walk(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["mask", "--map", str(tmp_path / "m.json"), "-f", "json", "--path", "$.user", "--stdin"],
        input=json.dumps({"user": {"email": EMAIL}, "other": "bob@example.com"}),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["user"]["email"] == "<PRIVYX_EMAIL_1>"
    assert payload["other"] == "bob@example.com"


def test_mask_jsonl_keeps_one_record_per_line(runner: CliRunner, tmp_path: Path) -> None:
    source = tmp_path / "in.jsonl"
    source.write_text(f'{{"e": "{EMAIL}"}}\n{{"e": "bob@example.com"}}\n', encoding="utf-8")
    out = tmp_path / "out.jsonl"

    result = runner.invoke(
        cli, ["mask", "--map", str(tmp_path / "m.json"), "-i", str(source), "-o", str(out)]
    )

    assert result.exit_code == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["e"] for line in lines] == ["<PRIVYX_EMAIL_1>", "<PRIVYX_EMAIL_2>"]


def test_unmask_leaves_unknown_tokens_alone(runner: CliRunner, tmp_path: Path) -> None:
    map_path = tmp_path / "m.json"
    runner.invoke(cli, ["mask", "--map", str(map_path), EMAIL])

    result = runner.invoke(cli, ["unmask", "--map", str(map_path), "<PRIVYX_EMAIL_99>"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "<PRIVYX_EMAIL_99>"


def test_unmask_without_a_mapping_source_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["unmask", "<PRIVYX_EMAIL_1>"])

    assert result.exit_code == 1
    assert "--map" in result.stderr


def test_unmask_unknown_session_exits_nonzero(runner: CliRunner, sqlite_config: Path) -> None:
    result = runner.invoke(cli, ["unmask", "-c", str(sqlite_config), "--session", "nope", "x"])

    assert result.exit_code == 1
    assert "session not found" in result.stderr


def test_mask_warns_when_the_mapping_cannot_survive_the_process(runner: CliRunner) -> None:
    """The default vault is `memory`, so without --map the output is unrecoverable."""
    result = runner.invoke(cli, ["mask", f"mail {EMAIL}"])

    assert result.exit_code == 0
    assert "memory" in result.stderr
    assert "--map" in result.stderr


def test_mask_without_input_exits_nonzero(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["mask"])

    assert result.exit_code == 1


# --------------------------------------------------------------------------
# proxy


def test_proxy_reload_restarts_and_rereads_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--reload` re-runs the server with the *new* config, then exits cleanly."""
    config = tmp_path / "c.yaml"
    config.write_text("port: 9001\n", encoding="utf-8")
    ports: list[int] = []

    async def fake_run_server(settings: object, watch_path: Path | None = None) -> bool:
        assert watch_path == config
        ports.append(settings.port)  # type: ignore[attr-defined]
        if len(ports) == 1:
            config.write_text("port: 9002\n", encoding="utf-8")
            return True  # as if the watcher had fired
        return False

    monkeypatch.setattr("privyx.cli.commands.proxy._run_server", fake_run_server)

    result = runner.invoke(cli, ["proxy", "--reload", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert ports == [9001, 9002]


def test_proxy_ssl_options_applied(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured_settings: list[object] = []

    async def fake_run_server(settings: object, watch_path: Path | None = None) -> bool:
        captured_settings.append(settings)
        return False

    monkeypatch.setattr("privyx.cli.commands.proxy._run_server", fake_run_server)

    result = runner.invoke(
        cli,
        [
            "proxy",
            "--ssl-certfile",
            "/tmp/cert.pem",
            "--ssl-keyfile",
            "/tmp/key.pem",
            "--ssl-keyfile-password",
            "mypass",
            "--ssl-ca-certs",
            "/tmp/ca.pem",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_settings) == 1
    s = captured_settings[0]
    assert s.tls.certfile == "/tmp/cert.pem"  # type: ignore[attr-defined]
    assert s.tls.keyfile == "/tmp/key.pem"  # type: ignore[attr-defined]
    assert s.tls.keyfile_password == "mypass"  # type: ignore[attr-defined]
    assert s.tls.ca_certs == "/tmp/ca.pem"  # type: ignore[attr-defined]
    assert s.is_tls  # type: ignore[attr-defined]


def test_proxy_ssl_requires_both_cert_and_key(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["proxy", "--ssl-certfile", "/tmp/cert.pem"])
    assert result.exit_code == 1
    assert "Both --ssl-certfile and --ssl-keyfile are required" in result.output


async def test_serve_transparent_wires_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("uvicorn")
    from privyx.cli.commands.proxy import _serve_transparent
    from privyx.config.loader import load_config

    captured: dict[str, object] = {}

    class Built(Exception):
        pass

    def fake_proxy(engine: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise Built  # stop before uvicorn starts

    monkeypatch.setattr("privyx.proxy.transparent.TransparentProxy", fake_proxy)
    settings = load_config(
        extra={
            "proxy": {"passthrough_unknown": False, "timeout": 42, "max_connections": 7},
            "provider": {"type": "openai", "openai_api_key": "sk-per-type"},
        }
    )

    with pytest.raises(Built):
        await _serve_transparent(settings, engine=None, audit=None)

    assert captured["passthrough_unknown"] is False
    assert captured["api_key"] == "sk-per-type"  # same resolution as the gateway
    assert captured["timeout"] == 42
    assert captured["connect_timeout"] == 10
    assert captured["max_connections"] == 7


@pytest.mark.parametrize("serve", ["_serve_transparent", "_serve_gateway"])
async def test_proxy_gives_running_requests_a_grace_period(
    serve: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("uvicorn")
    from privyx.cli.commands import proxy as proxy_command
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    captured: dict[str, object] = {}

    class Built(Exception):
        pass

    def fake_config(app: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise Built  # stop before uvicorn starts

    monkeypatch.setattr("uvicorn.Config", fake_config)
    settings = load_config()
    engine, close_vault = await build_engine(settings)
    try:
        with pytest.raises(Built):
            await getattr(proxy_command, serve)(settings, engine, audit=None)
    finally:
        await close_vault()

    assert captured["timeout_graceful_shutdown"] == proxy_command.SHUTDOWN_GRACE_SECONDS


async def test_a_signal_stop_lets_cut_requests_clean_up_first() -> None:
    """After SIGTERM, uvicorn cancels what outlived the grace period without
    waiting for it, then raises the signal again, which by default kills the
    process before the vault and the audit log close."""
    import asyncio
    import signal

    import httpx

    uvicorn = pytest.importorskip("uvicorn")
    from privyx.cli.commands.proxy import _serve_until_reload

    started, cleaned = asyncio.Event(), asyncio.Event()

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        started.set()
        try:
            await asyncio.sleep(60)  # a long stream
        finally:
            await asyncio.sleep(0.2)  # its ephemeral session's deletion
            cleaned.set()

    killed: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda sig, frame: killed.append(sig))
    server = uvicorn.Server(
        uvicorn.Config(
            app, port=0, log_level="warning", lifespan="off", timeout_graceful_shutdown=0.1
        )
    )
    try:
        serving = asyncio.create_task(_serve_until_reload(server, None))
        while not server.started:
            assert not serving.done()  # it failed to start
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/"):
                await started.wait()
                server.handle_exit(signal.SIGTERM, None)  # what the signal does
                await serving
    except httpx.HTTPError:
        pass  # the cut stream
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert cleaned.is_set()  # before _serve_until_reload returned
    assert not killed
