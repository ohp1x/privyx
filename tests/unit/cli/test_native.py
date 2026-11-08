"""Unit tests for native POSIX agent execution."""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from privyx.cli.launch import LaunchPlan, Target, build_launch_plan
from privyx.cli.native import (
    _serve_worker,
    _stop_worker,
    can_exec_agent,
    exec_agent,
    wait_for_agent_exit,
)
from privyx.core.errors import PrivyxError


def test_can_exec_agent_false_when_not_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    assert not can_exec_agent()


def test_can_exec_agent_false_when_threads_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("threading.active_count", lambda: 2)
    assert not can_exec_agent()


def test_can_exec_agent_false_when_stdin_not_atty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("threading.active_count", lambda: 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert not can_exec_agent()


def test_can_exec_agent_true_when_foreground(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("privyx.cli.native.threading.active_count", lambda: 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: 42)
    monkeypatch.setattr(os, "getpgrp", lambda: 42)
    assert can_exec_agent()


def test_can_exec_agent_false_when_background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("privyx.cli.native.threading.active_count", lambda: 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: 42)
    monkeypatch.setattr(os, "getpgrp", lambda: 99)
    assert not can_exec_agent()


def test_can_exec_agent_false_on_tcgetpgrp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("privyx.cli.native.threading.active_count", lambda: 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)

    def raise_err(_fd: int) -> int:
        raise OSError("Inappropriate ioctl for device")

    monkeypatch.setattr(os, "tcgetpgrp", raise_err)
    assert not can_exec_agent()


async def test_wait_for_agent_exit_returns_on_reparenting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ppids = [100, 100, 1]  # parent exits, reparented to 1

    def getppid() -> int:
        return ppids.pop(0) if ppids else 1

    monkeypatch.setattr(os, "getppid", getppid)
    await wait_for_agent_exit(100)


async def test_serve_worker_registers_signal_and_calls_serve() -> None:
    serve_mock = AsyncMock(return_value=0)
    ready = MagicMock(spec=socket.socket)
    res = await _serve_worker(serve_mock, ready, 123)
    assert res == 0
    serve_mock.assert_awaited_once_with(ready, 123)


def test_stop_worker_when_already_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (pid, 0))
    # Should return cleanly without kill
    _stop_worker(12345)


def test_stop_worker_reaps_with_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    calls = [0]

    def mock_waitpid(pid: int, flags: int) -> tuple[int, int]:
        calls[0] += 1
        if calls[0] == 1:
            return (0, 0)
        return (pid, 0)

    monkeypatch.setattr(os, "waitpid", mock_waitpid)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(sig))
    _stop_worker(12345)
    assert killed == [signal.SIGTERM]


def test_stop_worker_kills_with_sigkill_when_deadline_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (0, 0) if flags else (pid, 0))
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(sig))
    monkeypatch.setattr("time.monotonic", MagicMock(side_effect=[0.0, 10.0, 10.0]))
    _stop_worker(12345)
    assert signal.SIGKILL in killed


def test_stop_worker_handles_process_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_err(pid: int, flags: int) -> tuple[int, int]:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "waitpid", raise_err)
    _stop_worker(12345)


def _make_plan(cmd: str = "agent") -> LaunchPlan:
    from privyx.config.schema import Settings

    target = Target(command=cmd, adapter="none", provider="openai", env_vars=())
    return build_launch_plan(target, [], Settings(), 0, ())


def test_exec_agent_raises_on_fork_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _make_plan("agent")
    mock_fork = MagicMock(side_effect=OSError("Resource temporarily unavailable"))
    monkeypatch.setattr(os, "fork", mock_fork)
    with pytest.raises(OSError, match="Resource temporarily unavailable"):
        exec_agent(plan, AsyncMock(), MagicMock())


def test_exec_agent_raises_privyx_error_when_worker_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan("agent")
    r_sock, w_sock = socket.socketpair()
    monkeypatch.setattr(socket, "socketpair", lambda: (r_sock, w_sock))
    monkeypatch.setattr(os, "fork", lambda: 99999)
    monkeypatch.setattr("privyx.cli.native._stop_worker", lambda pid: None)

    # Worker writes error JSON
    w_sock.sendall(json.dumps({"error": "port already in use"}).encode() + b"\n")
    w_sock.close()

    with pytest.raises(PrivyxError, match="port already in use"):
        exec_agent(plan, AsyncMock(), MagicMock())
    r_sock.close()


def test_exec_agent_raises_privyx_error_when_worker_exits_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan("agent")
    r_sock, w_sock = socket.socketpair()
    monkeypatch.setattr(socket, "socketpair", lambda: (r_sock, w_sock))
    monkeypatch.setattr(os, "fork", lambda: 99999)
    monkeypatch.setattr("privyx.cli.native._stop_worker", lambda pid: None)

    # Worker closes without writing anything
    w_sock.close()

    with pytest.raises(PrivyxError, match="proxy exited before it finished starting"):
        exec_agent(plan, AsyncMock(), MagicMock())
    r_sock.close()


def test_exec_agent_raises_privyx_error_on_execvpe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan("missing-cmd")
    r_sock, w_sock = socket.socketpair()
    monkeypatch.setattr(socket, "socketpair", lambda: (r_sock, w_sock))
    monkeypatch.setattr(os, "fork", lambda: 99999)
    monkeypatch.setattr("privyx.cli.native._stop_worker", lambda pid: None)

    def mock_execvpe(cmd: str, argv: list[str], env: dict[str, str]) -> None:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(os, "execvpe", mock_execvpe)

    w_sock.sendall(json.dumps({"port": 8080}).encode() + b"\n")
    w_sock.close()

    with pytest.raises(PrivyxError, match="cannot launch 'missing-cmd': No such file or directory"):
        exec_agent(plan, AsyncMock(), MagicMock())
    r_sock.close()
