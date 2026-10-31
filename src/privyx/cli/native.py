"""Replace an interactive POSIX launcher with the agent, keeping its proxy alive."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import NoReturn

from privyx.cli.launch import LaunchPlan
from privyx.core.errors import PrivyxError


def can_exec_agent() -> bool:
    """Fork only before starting threads, in the controlling terminal's foreground."""
    if os.name != "posix" or threading.active_count() != 1 or not sys.stdin.isatty():
        return False
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return False


async def wait_for_agent_exit(pid: int) -> None:
    """The agent keeps the launcher's PID across exec; its proxy is its child.

    Reparenting detects exit even when the agent's descendants remain alive or
    close inherited file descriptors. Stopping and resuming the agent leaves
    the proxy running, as it would when using a separate `privyx proxy`.
    """
    while True:
        if os.getppid() != pid:
            return
        await asyncio.sleep(0.1)


async def _serve_worker(
    serve: Callable[[socket.socket, int], Awaitable[int]], ready: socket.socket, parent: int
) -> int:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    return await serve(ready, parent)


def _stop_worker(pid: int) -> None:
    """Reap a worker when startup or exec fails, with bounded cleanup time."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.02)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ChildProcessError, ProcessLookupError):
        pass


def exec_agent(
    plan: LaunchPlan,
    serve: Callable[[socket.socket, int], Awaitable[int]],
    announce: Callable[[LaunchPlan], None],
) -> NoReturn:
    """Start the proxy before exec, without changing the agent's terminal or PID."""
    parent = os.getpid()
    reader, writer = socket.socketpair()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        worker = os.fork()
    except OSError:
        reader.close()
        writer.close()
        raise
    if worker == 0:
        reader.close()
        code = 1
        try:
            os.setsid()  # Terminal signals belong to the agent, not its proxy.
            with open(os.devnull, "r+b", buffering=0) as null:
                for fd in (0, 1):
                    os.dup2(null.fileno(), fd)
            code = asyncio.run(_serve_worker(serve, writer, parent))
        except asyncio.CancelledError:
            code = 0
        except BaseException as exc:
            try:
                writer.sendall(json.dumps({"error": str(exc)}).encode() + b"\n")
            except OSError:
                pass  # Readiness was already sent, or the launcher exited.
        finally:
            writer.close()
            os._exit(code)

    writer.close()
    try:
        with reader.makefile("rb") as response:
            line = response.readline()
        if not line:
            raise PrivyxError("proxy exited before it finished starting")
        message = json.loads(line)
        if "error" in message:
            raise PrivyxError(message["error"])
        bound = plan.bind_port(message["port"])
        announce(bound)
        reader.close()  # No orchestration descriptors survive in the agent.
        try:
            os.execvpe(bound.command, [bound.command, *bound.argv], bound.environment)
        except OSError as exc:
            raise PrivyxError(f"cannot launch {bound.command!r}: {exc.strerror}") from exc
    except BaseException:
        _stop_worker(worker)
        raise
    finally:
        reader.close()
