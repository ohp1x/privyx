"""Tests for the CLI surface.

The CLI is orchestration only (principle #12), so these tests check that each
command reaches the *configured* components — the failure mode being a command
that quietly hardcodes its own detector or vault and ignores ``--config``.
"""

from __future__ import annotations

import base64
import json
import os
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


def test_detect_explain_names_the_detector_and_the_policy(
    runner: CliRunner, tmp_path: Path
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "detector:\n"
        "  - type: regex\n"
        "    terms:\n      PROJECT: [bluebird]\n"
        "  - type: yaml\n"
        "    patterns:\n      TICKET: 'TCK-\\d+'\n"
        "policy:\n  type: strict\n  allowed: [EMAIL, PROJECT]\n",
        encoding="utf-8",
    )
    text = f"mail {EMAIL} about bluebird, ticket TCK-42"
    result = runner.invoke(cli, ["detect", "--explain", "-c", str(config), text])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()

    def block(value: str) -> list[str]:
        start = next(i for i, line in enumerate(lines) if repr(value) in line)
        return lines[start + 1 : start + 3]

    assert block(EMAIL) == ["        found by  #1 regex: built-in pattern EMAIL", "        masked"]
    assert block("bluebird") == ["        found by  #1 regex: terms PROJECT", "        masked"]
    assert block("TCK-42") == [
        "        found by  #2 yaml: pattern TICKET",
        "        not masked: the strict policy masks only the types in policy.allowed",
    ]


def test_detect_explain_shows_a_partly_masked_span(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "detector:\n  type: yaml\n  patterns:\n    OUTER: abcdef\n    INNER: bcd\n"
        "policy:\n  type: strict\n  allowed: [INNER]\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli, ["detect", "--explain", "-c", str(config), "abcdef"])

    assert "OUTER" in result.output
    assert "partly masked: only 'bcd' as INNER; the rest is sent as is" in result.output
    assert "        masked" not in result.output


def test_detect_explain_names_an_opt_in_entity(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("detector:\n  type: regex\n  enable: [CRYPTO_WALLET]\n", encoding="utf-8")
    address = "0x" + "ab" * 20
    result = runner.invoke(cli, ["detect", "--explain", "-c", str(config), f"pay {address}"])

    assert result.exit_code == 0, result.output
    assert "found by  regex: built-in pattern CRYPTO_WALLET" in result.output


def test_detect_explain_names_a_decoded_finding(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("detector:\n  type: regex\n  decode: true\n", encoding="utf-8")
    hidden = base64.b64encode(EMAIL.encode()).decode()
    result = runner.invoke(cli, ["detect", "--explain", "-c", str(config), f"blob {hidden}"])

    assert result.exit_code == 0, result.output
    assert "found by  regex: built-in pattern EMAIL, once joined or decoded" in result.output


@pytest.mark.parametrize("decode", ["false", "true"])
def test_detect_explain_names_a_folded_finding(
    runner: CliRunner, tmp_path: Path, decode: str
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        f"detector:\n  type: regex\n  normalize: true\n  decode: {decode}\n", encoding="utf-8"
    )
    hidden = base64.b64encode(EMAIL.encode()).decode()
    text = f"mail ａｎａ@ｅｘａｍｐｌｅ.ｃｏｍ, blob {hidden}"
    result = runner.invoke(cli, ["detect", "--explain", "-c", str(config), text])

    assert result.exit_code == 0, result.output
    assert "found by  regex: built-in pattern EMAIL, once folded" in result.output
    decoded = "found by  regex: built-in pattern EMAIL, once joined or decoded"
    assert (decoded in result.output) == (decode == "true")


def test_detect_explain_without_policy_masks_everything(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["detect", "--explain", "--no-policy", f"mail {EMAIL}"])
    assert "found by  regex: built-in pattern EMAIL" in result.output
    assert "not masked" not in result.output


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


def test_session_show_sums_tokens_and_cost_from_the_audit_trail(
    runner: CliRunner, tmp_path: Path
) -> None:
    log = tmp_path / "audit.log"
    usage = {"input": 1_000_000, "output": 100_000, "cache_read": 3_000_000, "cache_write": 0}
    records = [
        {
            "event": "proxy.response",
            "session_id": "ses_a",
            "model": "claude-opus-5-5",
            "usage": usage,
        },
        {"event": "proxy.response", "session_id": "ses_a", "model": "gpt-x", "usage": usage},
        {"event": "proxy.response", "session_id": "ses_b", "model": "gpt-x", "usage": usage},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    config = tmp_path / "c.yaml"
    config.write_text(f"audit:\n  path: {log}\n", encoding="utf-8")

    result = runner.invoke(cli, ["session", "show", "-c", str(config), "ses_a"])

    assert result.exit_code == 0, result.output
    assert "not in the vault" in result.output
    assert "Responses:  2" in result.output
    assert "input 2,000,000, output 200,000, cache read 6,000,000" in result.output
    assert "(cache hit rate 75%)" in result.output
    # 1M input at $4 + 100k output at $20 + 3M cache reads at $0.20.
    assert "Cost:       ~$6.6000 (estimate; claude-opus-5-5)" in result.output
    assert "No price for: gpt-x (set audit.prices)" in result.output

    config.write_text(
        f"audit:\n  path: {log}\n  prices:\n    gpt-x: {{input: 1, output: 1}}\n",
        encoding="utf-8",
    )
    priced = runner.invoke(cli, ["session", "show", "-c", str(config), "ses_a"])
    assert "(estimate; claude-opus-5-5, gpt-x)" in priced.output


def test_session_show_finds_an_id_that_json_escapes(runner: CliRunner, tmp_path: Path) -> None:
    log = tmp_path / "audit.log"
    record = {"event": "proxy.response", "session_id": 'session"one', "usage": {"input": 5}}
    log.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    config = tmp_path / "c.yaml"
    config.write_text(f"audit:\n  path: {log}\n", encoding="utf-8")

    result = runner.invoke(cli, ["session", "show", "-c", str(config), 'session"one'])

    assert result.exit_code == 0, result.output
    assert "Responses:  1" in result.output


def test_inspect_token_names_its_session_masked(runner: CliRunner, sqlite_config: Path) -> None:
    session_id = _seed_session(sqlite_config)

    result = runner.invoke(cli, ["inspect", "token", "-c", str(sqlite_config), "<PRIVYX_EMAIL_1>"])

    assert result.exit_code == 0, result.output
    assert "Type: EMAIL" in result.output
    assert session_id in result.output
    assert EMAIL not in result.output
    assert "never (no vault.ttl)" in result.output


def test_inspect_token_reveal_and_missing(runner: CliRunner, sqlite_config: Path) -> None:
    _seed_session(sqlite_config)
    args = ["inspect", "token", "-c", str(sqlite_config)]

    assert EMAIL in runner.invoke(cli, [*args, "--reveal", "<PRIVYX_EMAIL_1>"]).output
    missing = runner.invoke(cli, [*args, "<PRIVYX_EMAIL_99>"])
    assert missing.exit_code == 1
    assert "Not found in any session" in missing.output


def test_inspect_routes_lists_the_configured_paths(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "proxy:\n  passthrough_unknown: false\n  routes:\n    /v1/messages: anthropic\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli, ["inspect", "routes", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert "/v1/messages  anthropic" in result.output
    assert "/v1/chat/completions" not in result.output
    assert "refused with 403" in result.output


@pytest.mark.parametrize(("provider", "gemini"), [("generic", False), ("google", True)])
def test_inspect_routes_shows_what_the_gateway_serves(
    runner: CliRunner, tmp_path: Path, provider: str, gemini: bool
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(f"proxy:\n  mode: gateway\nprovider:\n  type: {provider}\n")

    result = runner.invoke(cli, ["inspect", "routes", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert ("/*/models/*:generateContent" in result.output) is gemini
    assert "/api/chat" not in result.output
    assert "/v1/messages/count_tokens" not in result.output


def test_inspect_regex_by_entity_and_by_pattern(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("detector:\n  patterns:\n    ORDER: 'ORD-(\\d+)+X'\n", encoding="utf-8")
    args = ["inspect", "regex", "-c", str(config)]

    risky = runner.invoke(cli, [*args, "ORDER"])
    assert "Entity:  ORDER" in risky.output
    assert "ReDoS:   risky" in risky.output
    assert "µs per KB" in risky.output  # the sample does not set it off

    builtin = runner.invoke(cli, [*args, "EMAIL"])
    assert "no nested repeat found" in builtin.output
    assert "µs per KB" in builtin.output

    raw = runner.invoke(cli, [*args, r"EMP-\d{6}"])
    assert "Entity" not in raw.output and "no nested repeat found" in raw.output

    assert runner.invoke(cli, [*args, "("]).exit_code == 1


def test_inspect_regex_leaves_out_a_disabled_built_in(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("detector:\n  disable: [EMAIL]\n", encoding="utf-8")
    result = runner.invoke(cli, ["inspect", "regex", "-c", str(config), "EMAIL"])
    assert "Entity" not in result.output and "Pattern: EMAIL" in result.output


def test_inspect_regex_reports_a_stall_the_check_misses(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from privyx.cli.commands import inspect

    monkeypatch.setattr(inspect, "_BENCH_TIMEOUT", 0.5)
    result = runner.invoke(cli, ["inspect", "regex", "-c", os.devnull, "(?:.|..)+Z"])

    assert result.exit_code == 0, result.output
    assert "no nested repeat found" in result.output  # the static check misses it
    assert "stalled" in result.output


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
        {
            "ts": now,
            "event": "proxy.response",
            "request_id": "req_1",
            "duration_ms": 1500.0,
            "usage": {"input": 100, "output": 20, "cache_read": 300, "cache_write": 0},
        },
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
    assert "Tokens:     input 100, output 20, cache read 300, cache write 0" in result.output
    assert "(cache hit rate 75%)" in result.output
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


def test_audit_stats_counts_unknown_route_decisions(runner: CliRunner, tmp_path: Path) -> None:
    from privyx.observability.audit import AuditLogger

    log = tmp_path / "audit.log"
    with log.open("w") as writer:
        audit = AuditLogger(writer)
        audit.route_decision(allowed=True, method="POST")
        audit.route_decision(allowed=False, method="GET")
    result = runner.invoke(cli, ["audit", "stats", str(log)])
    assert result.exit_code == 0
    assert "Passthrough: 1 (body unmasked)" in result.output
    assert "Blocked:    1 (unknown route)" in result.output


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
    checks = (
        "plugins",
        "detector",
        "permissions",
        "vault",
        "provider",
        "proxy",
        "streaming",
        "routes",
        "configuration",
    )
    for check in checks:
        assert f"✓ {check}" in result.output


@pytest.mark.parametrize("unavailable", [False, True])
def test_doctor_resolves_secrets_once_per_invocation(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    from privyx.core.errors import ConfigError

    calls: list[str] = []

    def resolve(reference: str, path: str) -> str:
        calls.append(reference)
        if unavailable:
            raise ConfigError("password manager unavailable")
        return "example-credential"

    monkeypatch.setattr("privyx.security.secrets._op_read", resolve)
    config = tmp_path / "config.yaml"
    for item in ("first", "second"):
        reference = f"op://example/{item}/credential"
        config.write_text(f"provider:\n  api_key: {reference}\n")
        result = runner.invoke(cli, ["doctor", "-c", str(config)])
        assert result.exit_code == (1 if unavailable else 0), result.output
        if unavailable:
            assert "configuration: ConfigError: password manager unavailable" in result.output
            assert result.output.count("✗") == 1
        else:
            assert result.output.count("✓") == 9
        assert "example-credential" not in result.output
        assert calls.count(reference) == 1
    assert len(calls) == 2


def test_doctor_shares_settings_between_checks(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from privyx.config.schema import Settings

    module = importlib.import_module("privyx.cli.commands.doctor")
    snapshots: list[Settings] = []
    for name in (
        "plugins",
        "detector",
        "permissions",
        "vault",
        "provider",
        "proxy",
        "streaming",
        "routes",
        "configuration",
    ):
        original = getattr(module, f"_check_{name}")

        def record(settings: Settings, check=original) -> tuple[bool, str]:
            snapshots.append(settings)
            return check(settings)

        monkeypatch.setattr(module, f"_check_{name}", record)

    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert len(snapshots) == 9
    assert isinstance(snapshots[0], Settings)
    assert all(settings is snapshots[0] for settings in snapshots)


def test_doctor_covers_every_documented_check(runner: CliRunner) -> None:
    """All component checks plus effective route coverage are visible."""
    result = runner.invoke(cli, ["doctor"])
    assert result.output.count("✓") + result.output.count("✗") == 9


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


def test_doctor_detects_insecure_file_permissions(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "privyx.db"
    db.touch()
    db.chmod(0o644)
    config = tmp_path / "c.yaml"
    config.write_text(f"vault:\n  type: sqlite\n  dsn: {db}\n", encoding="utf-8")

    result = runner.invoke(cli, ["doctor", "-c", str(config)])

    assert result.exit_code == 1
    assert "✗ permissions" in result.output
    assert "chmod 600" in result.output


@pytest.mark.parametrize("passthrough", [False, True])
def test_doctor_reports_effective_route_coverage(
    runner: CliRunner, tmp_path: Path, passthrough: bool
) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text(f"proxy:\n  passthrough_unknown: {str(passthrough).lower()}\n")
    result = runner.invoke(cli, ["doctor", "-c", str(config)])
    assert result.exit_code == 0
    assert "/v1/responses (responses)" in result.output
    assert "/v1/messages/count_tokens (anthropic)" in result.output
    assert ("WITHOUT body masking" if passthrough else "blocked (403)") in result.output


def test_doctor_reports_invalid_routes(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text("proxy:\n  routes:\n    missing-slash: unsupported\n")
    result = runner.invoke(cli, ["doctor", "-c", str(config)])
    assert result.exit_code == 1
    assert "✗ routes: invalid path or unsupported schema" in result.output


def test_doctor_reports_gateway_auxiliary_gaps(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text("proxy:\n  mode: gateway\n")
    result = runner.invoke(cli, ["doctor", "-c", str(config)])
    assert result.exit_code == 0
    assert "unlisted paths return 404" in result.output
    assert "WITHOUT body masking" not in result.output
    assert "/responses/compact" in result.output.split("default paths outside coverage: ")[1]


@pytest.mark.parametrize(("provider", "served"), [("generic", False), ("google", True)])
def test_doctor_counts_gateway_gemini_routes_only_with_google(
    runner: CliRunner, tmp_path: Path, provider: str, served: bool
) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text(f"proxy:\n  mode: gateway\nprovider:\n  type: {provider}\n")
    result = runner.invoke(cli, ["doctor", "-c", str(config)])
    assert result.exit_code == 0
    covered = result.output.split("masking route(s): ")[1].split(";")[0]
    assert ("/*/models/*:generateContent (gemini)" in covered) is served


@pytest.mark.parametrize("mode", ["transparent", "gateway"])
def test_doctor_reports_a_replaced_route_map(runner: CliRunner, tmp_path: Path, mode: str) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text(f"proxy:\n  mode: {mode}\n  routes:\n    /v1/messages: anthropic\n")
    result = runner.invoke(cli, ["doctor", "-c", str(config)])
    assert result.exit_code == 0
    assert "1 masking route(s): /v1/messages (anthropic)" in result.output
    assert "/chat/completions" in result.output.split("default paths outside coverage: ")[1]


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
    assert "no agent profile 'some-tool'" in result.output
    assert "--env-var" in result.output
    assert "--provider openai or anthropic" in result.output


@pytest.mark.parametrize(
    ("provider", "injected"),
    [
        (
            "openai",
            [
                "OPENAI_API_BASE=http://127.0.0.1:<auto>/v1 [unknown command, provider openai]",
                "OPENAI_BASE_URL=http://127.0.0.1:<auto>/v1 [unknown command, provider openai]",
            ],
        ),
        (
            "anthropic",
            ["ANTHROPIC_BASE_URL=http://127.0.0.1:<auto> [unknown command, provider anthropic]"],
        ),
        (
            "google",
            ["GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:<auto> [unknown command, provider google]"],
        ),
    ],
)
def test_run_unknown_target_gets_the_usual_variables_of_its_provider(
    runner: CliRunner, provider: str, injected: list[str]
) -> None:
    result = runner.invoke(cli, ["run", "--dry-run", "-p", provider, "some-tool"])

    assert result.exit_code == 0, result.output
    env = result.output.split("Environment injected:\n")[1].splitlines()
    assert [line.strip() for line in env] == injected


def test_run_gemini_points_every_endpoint_at_privyx_and_refuses_unrouted_paths(
    runner: CliRunner,
) -> None:
    from privyx.config.loader import load_config
    from privyx.proxy.schemas import detect_schema

    result = runner.invoke(cli, ["run", "--dry-run", "gemini"])

    assert result.exit_code == 0, result.output
    assert "Upstream: https://generativelanguage.googleapis.com [" in result.output
    env = result.output.split("Environment injected:\n")[1].splitlines()
    assert [line.split("=")[0].strip() for line in env] == [
        "CODE_ASSIST_ENDPOINT",
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_VERTEX_BASE_URL",
    ]
    settings = load_config(None, agent="gemini")
    assert settings.proxy.passthrough_unknown is False
    # Google login and Vertex paths are not routed, so they are refused.
    for path in (
        "/v1internal:streamGenerateContent",
        "/v1/projects/p/locations/l/publishers/google/models/m:generateContent",
    ):
        assert detect_schema(path, settings.proxy.routes) is None
    assert load_config(None, agent="claude").proxy.passthrough_unknown is True


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("", "http://127.0.0.1:11434"),
        ("0.0.0.0", "http://127.0.0.1:11434"),  # what a server binds to
        (":9000", "http://127.0.0.1:9000"),
        ("gpu.example.test", "http://gpu.example.test:11434"),
        ("gpu.example.test:8080", "http://gpu.example.test:8080"),
        ("http://gpu.example.test", "http://gpu.example.test:80"),
        ("https://gpu.example.test/", "https://gpu.example.test:443"),
        ("[::1]:9000", "http://[::1]:9000"),
    ],
)
def test_ollama_host_is_read_as_ollama_reads_it(host: str, url: str) -> None:
    from privyx.cli.launch import ollama_url

    assert ollama_url(host) == url


def _dry_run(runner: CliRunner, *argv: str) -> dict[str, str]:
    result = runner.invoke(cli, ["run", "--dry-run", *argv])
    assert result.exit_code == 0, result.output
    head = result.output.split("Environment injected:\n")[0]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def test_run_ollama_model_runs_ollama_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    plan = _dry_run(runner, "ollama:llama3.2", "--verbose")

    assert plan["Profile"] == "ollama"
    assert plan["Command"] == "ollama run llama3.2 --verbose"
    assert plan["Upstream"] == "http://127.0.0.1:11434 [defaults]"
    result = runner.invoke(cli, ["run", "--dry-run", "ollama:llama3.2"])
    assert "OLLAMA_HOST=http://127.0.0.1:<auto>" in result.output
    error = runner.invoke(cli, ["run", "--dry-run", "ollama:"])
    assert error.exit_code != 0
    assert "name a model after ollama:" in error.output


def test_run_ollama_goes_to_the_server_ollama_host_names_unless_told_otherwise(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "gpu.example.test")

    from_env = _dry_run(runner, "ollama:qwen")["Upstream"]
    flag = _dry_run(runner, "-u", "http://other.test:1", "ollama:qwen")["Upstream"]
    config = tmp_path / "privyx.yaml"
    config.write_text("agents:\n  ollama:\n    upstream: http://cfg.test:11434\n")
    configured = _dry_run(runner, "-c", str(config), "ollama")["Upstream"]

    assert from_env == "http://gpu.example.test:11434 [environment OLLAMA_HOST]"
    assert flag.startswith("http://other.test:1 ")
    assert configured.startswith("http://cfg.test:11434 ")
    monkeypatch.setenv("OLLAMA_HOST", "gpu.example.test:port")
    assert "OLLAMA_HOST is not a valid address" in runner.invoke(cli, ["run", "ollama"]).output


def test_run_a_colon_profile_of_the_users_own_wins_over_ollama_model(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    config = tmp_path / "privyx.yaml"
    config.write_text('agents:\n  "ollama:work":\n    args: [run, mistral]\n')

    plan = _dry_run(runner, "-c", str(config), "ollama:work")

    assert plan["Profile"] == "ollama:work"
    assert plan["Command"] == "ollama run mistral"


@pytest.mark.skipif(os.name == "nt", reason="a shell script stands in for ollama")
def test_run_ollama_without_a_server_stops_before_starting_the_tool(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import socket

    marker = tmp_path / "started"
    tool = tmp_path / "ollama"
    tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    result = runner.invoke(cli, ["run", "-u", f"http://127.0.0.1:{port}", "ollama:llama3.2"])

    assert result.exit_code == 1
    assert f"no Ollama server answers at http://127.0.0.1:{port}" in result.output
    assert "ollama serve" in result.output
    assert not marker.exists()


def test_run_ollama_sends_no_key_of_privyx_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from privyx.config.loader import load_config
    from privyx.providers.registry import resolve_api_key

    monkeypatch.setenv("PRIVYX_API_KEY", "router-key")

    assert resolve_api_key(load_config(None, agent="ollama")) is None
    assert resolve_api_key(load_config(None)) == "router-key"


def test_check_ollama_reports_an_address_httpx_cannot_use(runner: CliRunner) -> None:
    from privyx.cli.commands.run import _check_ollama

    with pytest.raises(SystemExit) as exit_:
        _check_ollama("http://bad host:11434")
    assert exit_.value.code == 1


def test_check_ollama_passes_when_the_server_answers() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from privyx.cli.commands.run import _check_ollama

    class Server(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"error": "unauthorized"}'  # behind a proxy that wants a login
            self.send_response(401)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _check_ollama(f"http://127.0.0.1:{server.server_address[1]}")  # any answer will do
    finally:
        server.shutdown()
        server.server_close()


def test_run_unknown_target_with_env_var_gets_only_that_variable(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "--dry-run", "-p", "openai", "--env-var", "X", "some-tool"])

    assert result.exit_code == 0, result.output
    env = result.output.split("Environment injected:\n")[1].splitlines()
    assert [line.strip() for line in env] == ["X=http://127.0.0.1:<auto> [CLI]"]


def test_run_unknown_target_suggests_a_near_profile(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "claude-discovery"])

    assert result.exit_code != 0
    assert "did you mean 'claude'?" in result.output


def test_run_rejects_a_port_out_of_range(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "--port", "70000", "--env-var", "X", "--", "true"])

    assert result.exit_code == 2
    assert "0<=x<=65535" in result.output


def test_run_reports_a_port_in_use_in_one_line(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not uvicorn's log line followed by an unretrieved task exception."""
    import socket
    import sys

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        result = runner.invoke(
            cli,
            ["run", "--no-anchor", "--port", str(port), "--env-var", "X", "--", sys.executable],
        )

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert f"Error: cannot listen on 127.0.0.1:{port}: " in result.output


@pytest.mark.parametrize("variable", ["PRIVYX_LOG_FILE", "PRIVYX_AUDIT_PATH"])
def test_run_reports_an_unwritable_log_file_or_audit_path_in_one_line(
    variable: str, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    (tmp_path / "file").write_text("")  # a file where a directory is needed
    monkeypatch.setenv(variable, str(tmp_path / "file" / "privyx.log"))

    result = runner.invoke(cli, ["run", "--no-anchor", "--env-var", "X", "--", "true"])

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "Error: cannot write " in result.output


@pytest.mark.parametrize("log_file", [False, True])
def test_run_writes_nothing_to_the_tools_terminal(tmp_path: Path, log_file: bool) -> None:
    """The tool owns the terminal, so a warning would land on its screen.

    Run in a subprocess, as for real: pytest's own log handlers would keep
    logging from printing a warning to stderr as its last resort.  The three
    lines ``privyx run`` prints itself go to stderr, so stdout holds only what the
    tool printed and can be piped into a parser.
    """
    import os
    import subprocess
    import sys

    pytest.importorskip("uvicorn")

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.error, urllib.request\n"
        "request = urllib.request.Request(\n"
        "    os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=b'{\"messages\": []}',\n"
        "    headers={'content-type': 'application/json'},\n"
        ")\n"
        "try:\n"
        "    urllib.request.urlopen(request, timeout=30)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print('status', exc.code)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRIVYX_")}
    env["PRIVYX_AUDIT_ENABLED"] = "0"
    if log_file:
        env["PRIVYX_LOG_FILE"] = str(tmp_path / "privyx.log")
    # Nothing listens on port 9: the upstream is unreachable, which Privyx logs.
    command = ["run", "--no-anchor", "--provider", "anthropic", "--upstream", "http://127.0.0.1:9",
               "--env-var", "ANTHROPIC_BASE_URL", "--", sys.executable, str(probe)]  # fmt: skip
    result = subprocess.run(
        [sys.executable, "-c", "from privyx.cli.main import cli; cli()", *command],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )

    assert result.stdout == "status 502\n"
    started = [line.split()[0] for line in result.stderr.splitlines()]
    assert started == ["Privyx", "Running:", "ANTHROPIC_BASE_URL"]
    if log_file:
        assert "no response from the upstream" in (tmp_path / "privyx.log").read_text()


@pytest.mark.parametrize(
    ("host", "origin"), [("127.0.0.1", "http://127.0.0.1:"), ("::1", "http://[::1]:")]
)
async def test_run_spawns_target_against_a_live_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, origin: str
) -> None:
    """The spawned process really can reach the proxy through the injected URL.

    This is the whole point of ``privyx run``: an unmodified tool talks to what
    it thinks is the provider.  The child asserts on its own side and exits
    non-zero if the URL is unreachable, so a proxy that never started — or
    bound to a different port — fails here rather than at first use.
    """
    import socket
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    if ":" in host:
        try:
            socket.create_server((host, 0), family=socket.AF_INET6).close()
        except OSError:
            pytest.skip("no IPv6 loopback on this machine")
    monkeypatch.setenv("PRIVYX_HOST", host)

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
    assert recorded["url"].startswith(origin)


async def test_run_provisions_anchor_secret_by_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By default `privyx run` persists an anchor secret, so aliases stay stable."""
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
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
    anchor_key = home / ".privyx" / "anchor.key"
    assert anchor_key.exists()
    secret = anchor_key.read_text(encoding="utf-8").strip()
    assert len(secret) == 64
    int(secret, 16)  # valid hex


Seen = list[tuple[str, dict[str, str], str]]


def test_run_profile_routes_real_child_and_masks_payload(
    runner: CliRunner, tmp_path: Path, upstream: tuple[str, Seen], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML profile reaches the real runner, proxy and engine with argv intact."""
    import sys

    import yaml

    url, seen = upstream
    monkeypatch.setenv("WORK_SOURCE_TOKEN", "referenced-private-token")
    monkeypatch.setenv("CUSTOM_FLAG", "shell-value")
    probe = tmp_path / "probe.py"
    config = tmp_path / "profile.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "parent": {
                        "command": sys.executable,
                        "provider": "anthropic",
                        "upstream": url,
                        "base_path": "/v1",
                        "env_vars": ["PROBE_BASE_URL"],
                        "args": [str(probe), "--token", "actual-token"],
                        "env": {
                            "CUSTOM_FLAG": "profile-value",
                            "PROBE_BASE_URL": "https://bypass.example",
                        },
                    },
                    "work": {"extends": "parent", "env_from": {"AUTH_TOKEN": "WORK_SOURCE_TOKEN"}},
                },
                "audit": {"path": str(tmp_path / "audit.log")},
            }
        )
    )
    probe.write_text(
        "import os, sys, urllib.request\n"
        "assert sys.argv[1:] == ['--token', 'actual-token', '--cli-flag']\n"
        "assert os.environ['CUSTOM_FLAG'] == 'profile-value'\n"
        "assert os.environ['AUTH_TOKEN'] == 'referenced-private-token'\n"
        "assert os.environ['PROBE_BASE_URL'] == os.environ['EXTRA_BASE_URL']\n"
        "urllib.request.urlopen(urllib.request.Request(\n"
        "    os.environ['PROBE_BASE_URL'] + '/messages',\n"
        '    data=b\'{"messages": [{"role": "user", "content": "alice@example.com"}]}\',\n'
        "    headers={'content-type': 'application/json'},\n"
        "), timeout=5).read()\n"
        "sys.exit(7)\n"
    )
    result = runner.invoke(
        cli,
        [
            "run",
            "-c",
            str(config),
            "--no-anchor",
            "work",
            "--env-var",
            "EXTRA_BASE_URL",
            "--",
            "--cli-flag",
        ],
    )
    assert result.exit_code == 7, result.output
    assert len(seen) == 1
    assert seen[0][0] == "/v1/messages"
    assert EMAIL not in seen[0][2]
    assert "<PRIVYX_EMAIL_" in seen[0][2]
    assert "actual-token" not in result.output
    assert "referenced-private-token" not in result.output
    audit = (tmp_path / "audit.log").read_text()
    assert "referenced-private-token" not in audit and "profile-value" not in audit


async def test_run_relays_the_tools_own_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: tuple[str, Seen]
) -> None:
    """With no key configured, the tool's key or login reaches the upstream.

    Claude Code sends ``x-api-key``, or ``authorization`` for a subscription
    login, plus ``anthropic-beta`` for the features its body uses.  Dropping
    them answered every request with 401 ``x-api-key header is required``.
    """
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    for name in ("PRIVYX_API_KEY", "PRIVYX_ANTHROPIC_API_KEY", "PRIVYX_UPSTREAM_URL"):
        monkeypatch.delenv(name, raising=False)
    url, seen = upstream

    from privyx.cli.commands.run import TARGETS, Target, _run_target

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.request\n"
        "for auth in ({'x-api-key': 'sk-ant-client'}, {'authorization': 'Bearer oat-client'}):\n"
        "    urllib.request.urlopen(urllib.request.Request(\n"
        "        os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages?beta=true',\n"
        '        data=b\'{"messages": [{"role": "user", "content": "hi"}]}\',\n'
        "        headers={'content-type': 'application/json', 'anthropic-beta': 'b1', **auth},\n"
        "    ), timeout=5).read()\n",
        encoding="utf-8",
    )
    spec = Target(command=sys.executable, provider="anthropic", env_vars=TARGETS["claude"].env_vars)
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=None,
        upstream=url,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0
    assert [path for path, *_ in seen] == ["/v1/messages?beta=true"] * 2
    assert seen[0][1]["x-api-key"] == "sk-ant-client"
    assert seen[1][1]["authorization"] == "Bearer oat-client"
    assert all(headers["anthropic-beta"] == "b1" for _, headers, _ in seen)


async def test_run_serves_the_gateway_from_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: tuple[str, Seen]
) -> None:
    """``proxy.mode: gateway`` posts to the upstream URL as written, with Privyx's key.

    That is what an upstream behind a base path needs; the transparent proxy
    would keep only its origin.
    """
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("PRIVYX_ANTHROPIC_API_KEY", "sk-ant-privyx")
    monkeypatch.delenv("PRIVYX_UPSTREAM_URL", raising=False)
    url, seen = upstream
    config = tmp_path / "gateway.yaml"
    config.write_text(f"proxy:\n  mode: gateway\nupstream_url: {url}/base/v1/messages\n")

    from privyx.cli.commands.run import TARGETS, Target, _run_target

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.request\n"
        "urllib.request.urlopen(urllib.request.Request(\n"
        "    os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages',\n"
        '    data=b\'{"messages": [{"role": "user", "content": "hi"}]}\',\n'
        "    headers={'content-type': 'application/json'},\n"
        "), timeout=5).read()\n",
        encoding="utf-8",
    )
    spec = Target(command=sys.executable, provider="anthropic", env_vars=TARGETS["claude"].env_vars)
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=str(config),
        upstream=None,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0
    assert [path for path, *_ in seen] == ["/base/v1/messages"]
    assert seen[0][1]["x-api-key"] == "sk-ant-privyx"


# Each known tool reduced to how it finds its base URL.  Port 9 stands in for
# the provider the real tool would call when privyx run does not reach it.
FAKE_TOOL = """
import json, os, sys, urllib.request
args = sys.argv[1:]
if TOOL == "claude":  # --settings outranks settings.json, which outranks the environment
    settings = json.loads(args[args.index("--settings") + 1]) if "--settings" in args else {}
    base = settings.get("env", {}).get("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    path, body = "/v1/messages", {"messages": [{"role": "user", "content": TEXT}]}
elif TOOL == "codex":  # its config, not OPENAI_BASE_URL
    # A -c after the subcommand replaces every -c before it; past `--` nothing is a flag.
    flags = args[: args.index("--")] if "--" in args else args
    sub = flags[flags.index("exec") :] if "exec" in flags else []
    level = sub if any(arg.startswith(("-c", "--config")) for arg in sub) else flags
    config = [arg.split("=", 1)[1] for arg in level if arg.startswith("openai_base_url=")]
    base = json.loads(config[0]) if config else "http://127.0.0.1:9"
    path, body = "/responses", {"input": TEXT}
else:  # the OpenAI SDK, whose base URL ends in /v1
    base = os.environ["OPENAI_API_BASE" if TOOL == "aider" else "OPENAI_BASE_URL"]
    path, body = "/chat/completions", {"messages": [{"role": "user", "content": TEXT}]}
urllib.request.urlopen(urllib.request.Request(
    base + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
), timeout=5).read()
"""


@pytest.mark.parametrize(
    ("name", "argv", "path"),
    [
        ("claude", [], "/v1/messages"),
        ("codex", [], "/v1/responses"),
        ("codex", ["exec", "-c", 'model="m"', "hi"], "/v1/responses"),
        ("codex", ["exec", "--config=model=m", "--", "hi"], "/v1/responses"),
        ("openai", [], "/v1/chat/completions"),
        ("aider", [], "/v1/chat/completions"),
    ],
)
async def test_run_points_each_known_target_at_the_proxy(
    name: str,
    argv: list[str],
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: tuple[str, Seen],
) -> None:
    """Every known tool reaches Privyx, on a masked path.

    Codex reads its base URL from its config, not ``OPENAI_BASE_URL``, and drops
    the override given before a subcommand once the user's own arguments hold a
    ``-c`` after it; a base
    URL in Claude Code's settings.json outranks its environment; and the OpenAI
    SDK expects ``/v1`` in its base URL, without which its ``/chat/completions``
    is forwarded as is.
    """
    import sys
    from dataclasses import replace

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    for var in ("PRIVYX_API_KEY", "PRIVYX_ANTHROPIC_API_KEY", "PRIVYX_OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PRIVYX_UPSTREAM_URL", raising=False)
    url, seen = upstream

    from privyx.cli.commands.run import TARGETS, _run_target

    tool = tmp_path / name
    tool.write_text(
        f"#!{sys.executable}\nTOOL, TEXT = {name!r}, {f'mail {EMAIL}'!r}\n{FAKE_TOOL}",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    spec = replace(TARGETS[name], command=str(tool))
    code = await _run_target(
        spec=spec,
        argv=argv,
        config_path=None,
        upstream=url,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0, f"{name} did not reach Privyx"
    assert [seen_path for seen_path, *_ in seen] == [path]
    body = seen[0][2]
    assert EMAIL not in body
    assert "PRIVYX_EMAIL" in body


def test_run_repeats_the_codex_override_only_after_a_config_flag_of_the_users() -> None:
    from privyx.cli.launch import codex_args as _codex_args

    override = ["-c", 'openai_base_url="http://proxy/v1"']

    assert _codex_args("http://proxy/v1", ["exec", "hi"]) == [*override, "exec", "hi"]
    # An attached value counts; a `-c` past `--` is an argument, not a flag.
    assert _codex_args("http://proxy/v1", ["exec", "-cmodel=m", "--", "-c"]) == [
        *override, "exec", "-cmodel=m", *override, "--", "-c"
    ]  # fmt: skip


@pytest.mark.parametrize("requests", [0, 1])
async def test_run_warns_when_no_request_reached_privyx(
    requests: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    upstream: tuple[str, Seen],
) -> None:
    """A tool that calls its provider directly sends Privyx nothing at all."""
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("PRIVYX_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.delenv("PRIVYX_UPSTREAM_URL", raising=False)
    url, _ = upstream

    from privyx.cli.commands.run import Target, _run_target

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, urllib.request\n"
        f"for _ in range({requests}):\n"
        "    urllib.request.urlopen(urllib.request.Request(\n"
        "        os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=b'{\"messages\": []}',\n"
        "        headers={'content-type': 'application/json'},\n"
        "    ), timeout=5).read()\n",
        encoding="utf-8",
    )
    spec = Target(command=sys.executable, provider="anthropic", env_vars=("ANTHROPIC_BASE_URL",))
    code = await _run_target(
        spec=spec,
        argv=[str(probe)],
        config_path=None,
        upstream=url,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0
    warned = "Warning: no request from" in capsys.readouterr().err
    assert warned is (requests == 0)
    recorded = '"event": "run.unproxied"' in (tmp_path / "audit.log").read_text()
    assert recorded is (requests == 0)


async def test_run_keeps_the_audit_trail_out_of_the_working_directory(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool runs in the user's project, where an audit log is litter."""
    import sys

    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")

    for var in ("PRIVYX_AUDIT_PATH", "PRIVYX_AUDIT_ENABLED", "PRIVYX_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    from privyx.cli.commands.run import Target, _run_target

    spec = Target(command=sys.executable, provider="generic", env_vars=("PROBE_BASE_URL",))
    code = await _run_target(
        spec=spec,
        argv=["-c", "pass"],
        config_path=None,
        upstream=None,
        port=0,
        env_vars=spec.env_vars,
        session_strategy="ephemeral",
        no_anchor=True,
    )

    assert code == 0
    assert list(project.iterdir()) == []
    assert (home / ".privyx" / "audit.log").exists()


# --------------------------------------------------------------------------
# ~/.privyx


def test_the_default_database_is_in_the_home_directory(
    runner: CliRunner, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRIVYX_VAULT", "sqlite")

    result = runner.invoke(cli, ["session", "list"])

    assert result.exit_code == 0, result.output
    assert (home / ".privyx" / "privyx.db").exists()
    assert list(tmp_path.iterdir()) == []


def test_audit_stats_reads_the_home_directory_by_default(runner: CliRunner, home: Path) -> None:
    log = home / ".privyx" / "audit.log"
    log.parent.mkdir()
    _write_audit_log(log)

    assert "Requests:   1" in runner.invoke(cli, ["audit", "stats"]).output


def test_trust_allows_the_local_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    nothing = runner.invoke(cli, ["trust"])
    assert nothing.exit_code == 1
    assert "no privyx.yaml or .privyx/config.yaml" in nothing.output

    config = Path.cwd() / "privyx.yaml"
    config.write_text("port: 9001\n", encoding="utf-8")
    assert "Host: 127.0.0.1:8000" in runner.invoke(cli, ["config"]).output
    assert runner.invoke(cli, ["config", "--path"]).output.strip() == "defaults"

    result = runner.invoke(cli, ["trust"])

    assert result.exit_code == 0
    assert result.output.strip() == f"Trusted {config}"
    assert "Host: 127.0.0.1:9001" in runner.invoke(cli, ["config"]).output
    assert runner.invoke(cli, ["config", "--path"]).output.strip() == str(config)


@pytest.mark.parametrize("xdg", [False, True], ids=["default directories", "XDG directories"])
def test_files_from_earlier_locations_move_into_the_home_directory(
    runner: CliRunner, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: bool
) -> None:
    from privyx.security.keys import read_or_create_anchor_secret

    config_dir, state_dir = home / ".config", home / ".local" / "state"
    if xdg:
        config_dir, state_dir = tmp_path / "xdg-config", tmp_path / "xdg-state"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    anchor, audit = config_dir / "privyx" / "anchor.key", state_dir / "privyx" / "audit.log"
    for path in (anchor, audit):
        path.parent.mkdir(parents=True)
    anchor.write_text("a" * 64, encoding="utf-8")
    anchor.chmod(0o600)
    _write_audit_log(audit)

    result = runner.invoke(cli, ["audit", "stats"])

    assert result.exit_code == 0, result.output
    assert "Requests:   1" in result.output  # the trail is read where it now is
    assert f"Moved {anchor} to {home / '.privyx' / 'anchor.key'}" in result.output
    assert not anchor.exists()
    assert not audit.exists()
    assert (home / ".privyx" / "anchor.key").stat().st_mode & 0o777 == 0o600
    assert read_or_create_anchor_secret() == "a" * 64  # pseudonyms stay what they were


def test_a_file_the_home_directory_already_has_is_not_replaced(
    runner: CliRunner, home: Path
) -> None:
    earlier, current = home / ".config" / "privyx" / "anchor.key", home / ".privyx" / "anchor.key"
    for path, secret in ((earlier, "a" * 64), (current, "b" * 64)):
        path.parent.mkdir(parents=True)
        path.write_text(secret, encoding="utf-8")

    result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0, result.output
    assert "Moved" not in result.output
    assert earlier.read_text(encoding="utf-8") == "a" * 64
    assert current.read_text(encoding="utf-8") == "b" * 64


# --------------------------------------------------------------------------
# config


def test_config_show_outputs_settings(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["config", "--show"])

    assert result.exit_code == 0
    assert "detector" in result.output or "vault" in result.output


def test_config_sources_reports_winning_layers(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    included = tmp_path / "included.yaml"
    included.write_text("port: 9001\nvault:\n  ttl: 60\n  type: sqlite\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("include: included.yaml\nport: 9002\nvault:\n  ttl: 120\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_PORT", "9003")

    result = runner.invoke(cli, ["-c", str(config), "config", "--sources"])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["port"] == {"value": 9003, "source": "environment"}
    assert shown["vault.ttl"] == {"value": 120, "source": str(config)}
    assert shown["vault.type"] == {"value": "sqlite", "source": str(included)}
    assert shown["host"] == {"value": "127.0.0.1", "source": "defaults"}
    assert shown["provider.api_key"] == {"value": "", "source": "defaults"}
    assert shown["plugins.paths"] == {"value": [], "source": "defaults"}


@pytest.mark.parametrize("environment", [False, True])
def test_config_sources_tracks_fields_in_detector_lists(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: bool
) -> None:
    included = tmp_path / "included.yaml"
    included.write_text(
        "detector:\n  - type: yaml\n    cache: true\n    terms: {PERSON: [john smith]}\n"
        "  - type: regex\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text("include: included.yaml\ndetector:\n  cache: true\n", encoding="utf-8")
    if environment:
        monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    for index in (0, 1):
        assert shown[f"detector.{index}.cache.enabled"] == {
            "value": not environment,
            "source": "environment" if environment else str(config),
        }
        assert shown[f"detector.{index}.cache.max_size"] == {"value": 10000, "source": "defaults"}
        assert shown[f"detector.{index}.type"]["source"] == str(included)
        assert shown[f"detector.{index}.normalize"] == {"value": False, "source": "defaults"}
    assert shown["detector.0.terms.PERSON"] == {"value": ["***"], "source": str(included)}


def test_config_sources_discards_origins_of_replaced_detector_lists(
    runner: CliRunner, tmp_path: Path
) -> None:
    (tmp_path / "included.yaml").write_text(
        "detector:\n  - type: yaml\n    cache: false\n  - type: regex\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text("include: included.yaml\ndetector:\n  - type: regex\n", encoding="utf-8")

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["detector.0.type"] == {"value": "regex", "source": str(config)}
    assert shown["detector.0.cache.enabled"] == {"value": True, "source": "defaults"}
    assert not any(path.startswith("detector.1.") for path in shown)


def test_config_sources_tracks_inherited_profile_fields(runner: CliRunner, tmp_path: Path) -> None:
    included = tmp_path / "included.yaml"
    included.write_text(
        "agents:\n  base:\n    command: example\n    privyx:\n"
        "      session:\n        strategy: client\n"
        "      detector:\n        - type: yaml\n        - type: regex\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "include: included.yaml\nagents:\n  work:\n    extends: base\n"
        "    privyx:\n      detector:\n        cache: false\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["agents.work.command"] == {"value": "example", "source": str(included)}
    assert shown["agents.work.extends"] == {"value": "base", "source": str(config)}
    assert shown["agents.work.privyx.session.strategy"] == {
        "value": "client",
        "source": str(included),
    }
    assert shown["agents.work.privyx.detector.0.type"] == {
        "value": "yaml",
        "source": str(included),
    }
    assert shown["agents.work.privyx.detector.0.cache"] == {
        "value": False,
        "source": str(config),
    }


def test_config_sources_clears_shorthand_when_replaced_by_mapping(
    runner: CliRunner, tmp_path: Path
) -> None:
    included = tmp_path / "included.yaml"
    included.write_text("detector:\n  cache: false\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "include: included.yaml\ndetector:\n  cache:\n    max_size: 200\n", encoding="utf-8"
    )

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["detector.cache.enabled"] == {"value": True, "source": "defaults"}
    assert shown["detector.cache.max_size"] == {"value": 200, "source": str(config)}


@pytest.mark.parametrize("flags", [[], ["--show"], ["--path"], ["--show", "--path"]])
def test_config_sources_takes_precedence_over_show_and_path(
    runner: CliRunner, flags: list[str]
) -> None:
    result = runner.invoke(cli, ["config", "--sources", *flags])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["port"] == {"value": 8000, "source": "defaults"}


def test_config_schema_takes_precedence_over_sources(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVYX_PORT", "invalid")
    result = runner.invoke(cli, ["config", "--sources", "--schema"])

    assert result.exit_code == 0, result.output
    assert "properties" in json.loads(result.output)


def test_config_sources_does_not_print_secret_values_in_errors(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" + "proj-abc123"
    monkeypatch.setenv("TEST_SECRET_PORT", secret)
    config = tmp_path / "config.yaml"
    config.write_text("port: ${TEST_SECRET_PORT}\nprovider:\n  api_key: ${TEST_SECRET_PORT}\n")

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 1
    assert "Error: invalid configuration" in result.output
    assert "Traceback" not in result.output
    assert secret not in result.output


def test_config_sources_masks_resolved_secrets(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" + "proj-abc123"
    calls: list[str] = []

    def resolve(reference: str, path: str) -> str:
        calls.append(path)
        return secret

    monkeypatch.setattr("privyx.security.secrets._op_read", resolve)
    config = tmp_path / "config.yaml"
    config.write_text("provider:\n  api_key: op://example/item/credential\n", encoding="utf-8")

    result = runner.invoke(cli, ["config", "--sources", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert calls == ["provider.api_key"]
    assert secret not in result.output
    assert "op://" not in result.output
    assert json.loads(result.output)["provider.api_key"] == {"value": "***", "source": str(config)}


def test_config_path_names_the_files_in_use_or_defaults(
    runner: CliRunner, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["config", "--path"]).output.strip() == "defaults"

    named = tmp_path / "custom.yaml"
    named.write_text("port: 9000\n", encoding="utf-8")
    result = runner.invoke(cli, ["config", "--path", "-c", str(named)])
    assert result.exit_code == 0
    assert result.output.strip() == str(named)

    home_config = home / ".privyx" / "config.yaml"
    home_config.parent.mkdir()
    home_config.write_text("port: 9001\n", encoding="utf-8")
    assert runner.invoke(cli, ["config", "--path"]).output.strip() == str(home_config)

    included = tmp_path / "extra.yaml"
    included.write_text("log_level: debug\n", encoding="utf-8")
    project = tmp_path / "privyx.yaml"
    project.write_text("include: extra.yaml\nport: 9002\n", encoding="utf-8")
    assert runner.invoke(cli, ["config", "--path"]).output.strip() == str(home_config)

    trusted = runner.invoke(cli, ["trust"])
    assert trusted.exit_code == 0, trusted.output
    result = runner.invoke(cli, ["config", "--path"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [str(home_config), str(included), str(project)]


@pytest.mark.parametrize("reference", ["op://example/item/credential", "keychain://example/account"])
def test_config_path_does_not_resolve_secrets(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    from privyx.core.errors import ConfigError

    def unavailable(reference: str, path: str) -> str:
        raise ConfigError("password manager unavailable")

    monkeypatch.setattr("privyx.security.secrets._op_read", unavailable)
    monkeypatch.setattr("privyx.security.secrets._keychain", unavailable)
    included = tmp_path / "credentials.yaml"
    included.write_text(f"provider:\n  api_key: {reference}\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("include: credentials.yaml\n", encoding="utf-8")

    result = runner.invoke(cli, ["config", "--path", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [str(included), str(config)]


def test_config_path_does_not_validate_settings(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("port: invalid\n", encoding="utf-8")
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "bogus")

    result = runner.invoke(cli, ["-c", str(config), "config", "--path"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(config)


@pytest.mark.parametrize("content", [None, "include: missing.yaml\n", "port: [\n"])
def test_config_path_reports_file_errors_without_traceback(
    runner: CliRunner, tmp_path: Path, content: str | None
) -> None:
    config = tmp_path / "config.yaml"
    if content is not None:
        config.write_text(content, encoding="utf-8")

    result = runner.invoke(cli, ["config", "--path", "-c", str(config)])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [["config"], ["config", "--show"], ["config", "--show", "--path"]])
def test_config_reports_bad_config_without_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "bogus")

    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    assert "Error: invalid configuration" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("flag", ["--show", "--sources"])
def test_config_show_masks_secrets(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
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

    result = runner.invoke(cli, ["config", flag, "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert not [s for s in secrets if s in result.output]
    shown = json.loads(result.output)
    if flag == "--sources":
        assert shown["vault.redis_url"]["value"] == "redis://:***@cache:6379/0"
        assert shown["provider.openai_api_key"] == {"value": "***", "source": "environment"}
        assert shown["provider.api_key"] == {"value": "", "source": "defaults"}
        assert shown["detector.0.patterns.CODENAME"]["value"] == "***"
        assert shown["detector.0.terms.PERSON"]["value"] == ["***"]
        return
    assert shown["vault"]["redis_url"] == "redis://:***@cache:6379/0"
    assert shown["provider"]["openai_api_key"] == "***"
    assert shown["provider"]["api_key"] == ""  # unset stays visible as unset
    assert shown["detector"][0]["patterns"] == {"CODENAME": "***"}  # names stay
    assert shown["detector"][0]["terms"] == {"PERSON": ["***"]}


def test_cli_exposes_every_documented_command(runner: CliRunner) -> None:
    """The CLI tree: proxy, run, detect, mask, unmask, inspect, session, audit, config, doctor,
    trust."""
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
        "trust",
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


def test_mask_creates_the_map_file_owner_only(runner: CliRunner, tmp_path: Path) -> None:
    """The map holds the original values."""
    map_path = tmp_path / "m.json"
    umask = os.umask(0o022)  # a plain write would then create it 0644
    try:
        result = runner.invoke(cli, ["mask", "--map", str(map_path), f"mail {EMAIL}"])
    finally:
        os.umask(umask)

    assert result.exit_code == 0
    assert map_path.stat().st_mode & 0o777 == 0o600


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


def test_mask_json_sees_a_value_with_its_key(runner: CliRunner, tmp_path: Path) -> None:
    """``{"password": "hunter2"}`` is a secret by its key, also in a list under it
    and in JSON Lines; a number is checked alone, and masked becomes its token."""
    record = {
        "password": "hunter2",
        "pins": ["letmein"],
        "card": 4111111111111111,
        "token_limit": 4000,
    }
    for fmt, source in (("json", json.dumps(record)), ("jsonl", json.dumps(record) + "\n")):
        result = runner.invoke(
            cli,
            ["mask", "--map", str(tmp_path / f"{fmt}.json"), "-f", fmt, "--stdin"],
            input=source,
        )

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "password": "<PRIVYX_SECRET_1>",
            "pins": ["letmein"],  # `pins` names no secret
            "card": "<PRIVYX_CREDIT_CARD_2>",
            "token_limit": 4000,
        }


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

    async def fake_run_server(settings: object, watch_paths: list[Path] | None = None) -> bool:
        assert watch_paths == [config]
        ports.append(settings.port)  # type: ignore[attr-defined]
        if len(ports) == 1:
            config.write_text("port: 9002\n", encoding="utf-8")
            return True  # as if the watcher had fired
        return False

    monkeypatch.setattr("privyx.cli.commands.proxy._run_server", fake_run_server)

    result = runner.invoke(cli, ["proxy", "--reload", "-c", str(config)])

    assert result.exit_code == 0, result.output
    assert ports == [9001, 9002]


def test_proxy_reload_watches_every_config_file_in_use(
    runner: CliRunner, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    home_config = home / ".privyx" / "config.yaml"
    home_config.parent.mkdir()
    home_config.write_text("port: 9001\n", encoding="utf-8")
    local = Path.cwd() / "privyx.yaml"
    local.write_text("log_level: debug\n", encoding="utf-8")
    assert runner.invoke(cli, ["trust"]).exit_code == 0
    watched: list[list[Path] | None] = []

    async def fake_run_server(settings: object, watch_paths: list[Path] | None = None) -> bool:
        watched.append(watch_paths)
        return False

    monkeypatch.setattr("privyx.cli.commands.proxy._run_server", fake_run_server)

    result = runner.invoke(cli, ["proxy", "--reload"])

    assert result.exit_code == 0, result.output
    assert watched == [[home_config, local]]


def test_reload_notices_a_change_to_any_watched_file(tmp_path: Path) -> None:
    from privyx.cli.commands.proxy import _mtimes

    paths = [tmp_path / "a.yaml", tmp_path / "b.yaml"]
    for path in paths:
        path.write_text("port: 9001\n", encoding="utf-8")
    before = _mtimes(paths)

    os.utime(paths[1], ns=(1, 1))
    assert _mtimes(paths) not in (None, before)

    paths[1].unlink()  # between an editor's delete and its write
    assert _mtimes(paths) is None


def test_proxy_ssl_options_applied(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured_settings: list[object] = []

    async def fake_run_server(settings: object, watch_paths: list[Path] | None = None) -> bool:
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
