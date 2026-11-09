"""Tests for `privyx pipe`."""

from __future__ import annotations

import asyncio
import io
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.commands import pipe
from privyx.cli.main import cli

EMAIL = "alice@example.com"


@pytest.fixture
def config(tmp_path: Path) -> Path:
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


def test_masks_and_restores_lines_byte_for_byte(config: Path) -> None:
    runner = CliRunner()
    source = f"a {EMAIL}\r\nno value \xff\nb {EMAIL}".encode("latin-1")
    masked = runner.invoke(cli, ["pipe", "-c", str(config), "--session", "s1"], input=source)
    assert masked.exit_code == 0, masked.output
    assert masked.stderr == "session: s1\n"
    assert EMAIL not in masked.stdout
    # Line endings are kept; a byte that is not UTF-8 is replaced, not fatal.
    # stdout_bytes: click's Result.stdout turns CRLF into LF.
    token = "<PRIVYX_EMAIL_1>"
    assert masked.stdout_bytes.decode() == f"a {token}\r\nno value \ufffd\nb {token}"

    restored = runner.invoke(
        cli, ["pipe", "-c", str(config), "--session", "s1", "--restore"], input=masked.stdout_bytes
    )
    assert restored.exit_code == 0, restored.output
    assert restored.stdout_bytes.decode() == f"a {EMAIL}\r\nno value \ufffd\nb {EMAIL}"


def test_restore_needs_a_session(config: Path) -> None:
    result = CliRunner().invoke(cli, ["pipe", "-c", str(config), "--restore"], input="x\n")
    assert result.exit_code == 1
    assert "--restore needs --session" in result.stderr


def test_restore_of_an_unknown_session_fails(config: Path) -> None:
    result = CliRunner().invoke(
        cli, ["pipe", "-c", str(config), "--session", "nope", "--restore"], input="x\n"
    )
    assert result.exit_code == 1
    assert "session not found: nope" in result.stderr


def _cli(*args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "from privyx.cli.main import cli; cli()", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.parametrize("operator", ["pseudonym", "fpe"])
def test_restore_follows_a_masking_pipe_on_a_new_session(config: Path, operator: str) -> None:
    """`pipe | ... | pipe --restore` on a session neither has seen yet.

    Both start together, so restore must not look the session up before its
    first line, and must see what masking adds after it started, also when
    the substitute is a literal value rather than a token.
    """
    with config.open("a", encoding="utf-8") as fh:
        fh.write(f"operator:\n  type: {operator}\n")
    runner = CliRunner()
    proc = _cli("pipe", "-c", str(config), "--session", "new", "--restore")
    assert proc.stdin is not None and proc.stdout is not None
    try:
        time.sleep(1.5)  # past start-up, where it used to look the session up
        assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
        for value in (EMAIL, "bob@example.com"):
            masked = runner.invoke(
                cli, ["pipe", "-c", str(config), "--session", "new"], input=f"{value}\n"
            )
            assert masked.exit_code == 0, masked.output
            assert value not in masked.stdout
            proc.stdin.write(masked.stdout_bytes)
            proc.stdin.flush()
            assert proc.stdout.readline() == f"{value}\n".encode()
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()


def test_masking_keeps_what_another_writer_added(config: Path) -> None:
    """A long-running pipe must not mask from, and save, a stale copy of its session."""
    runner = CliRunner()
    proc = _cli("pipe", "-c", str(config), "--session", "s1")
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(f"{EMAIL}\n".encode())
        proc.stdin.flush()
        assert proc.stdout.readline() == b"<PRIVYX_EMAIL_1>\n"
        other = runner.invoke(
            cli, ["pipe", "-c", str(config), "--session", "s1"], input="bob@example.com\n"
        )
        assert other.stdout == "<PRIVYX_EMAIL_2>\n"
        proc.stdin.write(b"carol@example.com\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == b"<PRIVYX_EMAIL_3>\n"
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    restored = runner.invoke(
        cli,
        ["pipe", "-c", str(config), "--session", "s1", "--restore"],
        input="<PRIVYX_EMAIL_1> <PRIVYX_EMAIL_2> <PRIVYX_EMAIL_3>\n",
    )
    assert restored.stdout == f"{EMAIL} bob@example.com carol@example.com\n"


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_pipe_stops_when_the_session_vanishes(config: Path, restore: bool, expired: bool) -> None:
    """A pruned or expired session is not restored from what was read before."""
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine

    async def delete() -> None:
        engine, close = await build_engine(load_config(config))
        try:
            assert await engine.delete_session("s1", reason="test")
        finally:
            await close()

    if expired:
        config.write_text(
            config.read_text().replace("  type: sqlite", "  type: sqlite\n  ttl: 3600")
        )

    runner = CliRunner()
    runner.invoke(cli, ["pipe", "-c", str(config), "--session", "s1"], input=f"{EMAIL}\n")
    proc = _cli("pipe", "-c", str(config), "--session", "s1", *(["--restore"] if restore else []))
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        proc.stdin.write(b"<PRIVYX_EMAIL_1>\n" if restore else f"{EMAIL}\n".encode())
        proc.stdin.flush()
        assert proc.stdout.readline() == (
            f"{EMAIL}\n".encode() if restore else b"<PRIVYX_EMAIL_1>\n"
        )
        if expired:
            import sqlite3

            with sqlite3.connect(config.parent / "privyx.db") as db:
                db.execute("UPDATE sessions SET updated_at = 0 WHERE session_id = ?", ("s1",))
        else:
            asyncio.run(delete())
        proc.stdin.write(b"<PRIVYX_EMAIL_1>\n" if restore else b"ana@example.com\n")
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 1
        assert proc.stdout.read() == b""
        assert b"session not found: s1" in proc.stderr.read()
    finally:
        proc.kill()


def test_a_read_error_ends_the_command(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Failing(io.BytesIO):
        def readline(self, size: int | None = -1) -> bytes:
            if self.tell():
                raise OSError("input/output error")
            return super().readline(size)

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(Failing(f"{EMAIL}\nmore\n".encode())))
    with pytest.raises(SystemExit) as exit_:
        pipe.pipe.main(["-c", str(config)], standalone_mode=False)
    assert exit_.value.code == 1
    out, err = capsys.readouterr()
    assert out == "<PRIVYX_EMAIL_1>\n"
    assert "Error: input/output error" in err


def test_ctrl_c_stops_a_pipe_whose_reader_stopped_reading(tmp_path: Path) -> None:
    """Blocked writing to a full stdout pipe, Ctrl-C still ends it."""
    config = tmp_path / "memory.yaml"
    config.write_text("detector:\n  type: regex\n", encoding="utf-8")
    proc = _cli("pipe", "-c", str(config))
    assert proc.stdin is not None
    try:
        # 80 KB through a 64 KB stdout pipe nobody reads; what is left of the
        # input fits in the stdin pipe, so this write returns.
        proc.stdin.write((b"x" * 99 + b"\n") * 800)
        proc.stdin.flush()
        time.sleep(1.5)  # until it blocks on the full pipe
        assert proc.poll() is None
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
    finally:
        proc.kill()


def test_each_line_is_written_before_the_next_is_read(config: Path) -> None:
    """Run as for real: a line comes out while stdin is still open, and Ctrl-C ends it."""
    proc = _cli("pipe", "-c", str(config))
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(f"mail {EMAIL}\n".encode())
        proc.stdin.flush()
        assert proc.stdout.readline() == b"mail <PRIVYX_EMAIL_1>\n"
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
    finally:
        proc.kill()


def test_concurrent_pipe_processes_keep_distinct_mappings(config: Path) -> None:
    initialized = CliRunner().invoke(
        cli, ["pipe", "-c", str(config), "--session", "shared"], input=""
    )
    assert initialized.exit_code == 0, initialized.output
    # Slow masking after the session read to expose overlapping updates reliably.
    script = """
import asyncio
from privyx.core.engine import PrivacyEngine
from privyx.cli.main import cli
original = PrivacyEngine.transform
async def slow(self, *args, **kwargs):
    await asyncio.sleep(0.2)
    return await original(self, *args, **kwargs)
PrivacyEngine.transform = slow
cli()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, "pipe", "-c", str(config), "--session", "shared"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    values = ["ana@example.com", "bob@example.com"]
    outputs = []
    try:
        for proc in processes:
            assert proc.stderr is not None
            assert proc.stderr.readline() == b"session: shared\n"
        for proc, value in zip(processes, values, strict=True):
            assert proc.stdin is not None
            proc.stdin.write(f"{value}\n".encode())
            proc.stdin.flush()
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=10)
            assert proc.returncode == 0, stderr
            outputs.append(stdout)
        assert len(set(outputs)) == 2
        for output, value in zip(outputs, values, strict=True):
            restored = CliRunner().invoke(
                cli,
                ["pipe", "-c", str(config), "--session", "shared", "--restore"],
                input=output,
            )
            assert restored.exit_code == 0, restored.output
            assert restored.stdout == f"{value}\n"
    finally:
        for proc in processes:
            proc.kill()
            proc.wait(timeout=10)
