"""`privyx pipe` — mask or restore stdin line by line, for Unix pipelines."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any, NoReturn

import click

from privyx.cli.config import config_options


@click.command()
@click.option("--session", "session_id", default=None, help="Vault session id")
@click.option("--restore", is_flag=True, help="Put the original values back instead of masking")
@config_options
def pipe(session_id: str | None, restore: bool, config_path: str | None) -> None:
    """Mask stdin to stdout one line at a time.

    Each line is written as soon as it is read, so `tail -f app.log | privyx
    pipe | ...` never waits for the end of the input.  A value split across
    lines, like a PEM private key, is not seen whole: use `privyx mask` for
    those.  --restore puts the original values back, given the --session the
    lines were masked with.
    """
    from privyx.core.errors import PrivyxError

    if restore and not session_id:
        click.echo("Error: --restore needs --session ID", err=True)
        sys.exit(1)
    # Ctrl-C raises at once, even in a read or write that blocks: asyncio.run's
    # own handler only cancels the task, which waits for that call to return,
    # so a pipe that gets no more input, or whose reader stopped reading,
    # would never stop.  asyncio.run keeps a handler that is not Python's own.
    previous = signal.signal(signal.SIGINT, _interrupt)
    try:
        asyncio.run(_pipe(session_id, restore, config_path))
    except KeyboardInterrupt:
        _drop_stdout()
        sys.exit(130)
    except BrokenPipeError:
        _drop_stdout()  # the reader went away (`| head`): stop quietly
    except (PrivyxError, OSError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    finally:
        signal.signal(signal.SIGINT, previous)


def _interrupt(signum: int, frame: Any) -> NoReturn:
    raise KeyboardInterrupt


def _drop_stdout() -> None:
    """Send what is left in stdout's buffer nowhere.

    Python flushes it at exit, which would fail again on a closed pipe, or
    block on a full one that nobody reads.
    """
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


async def _pipe(session_id: str | None, restore: bool, config_path: str | None) -> None:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine
    from privyx.core.context import Context
    from privyx.core.errors import SessionNotFoundError
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    load_plugins(settings)
    if not restore and settings.vault.type == "memory":
        click.echo(
            "Warning: vault type is 'memory', so this output can never be restored. "
            "Configure a sqlite/redis vault to keep the mapping.",
            err=True,
        )
    engine, close = await build_engine(settings)
    try:
        if restore:
            sid = session_id or ""
            context = Context(session_id=sid, vault=engine.vault)
        else:
            sid = (await engine.get_or_create_session(session_id)).session_id
            click.echo(f"session: {sid}", err=True)
        source, out = sys.stdin.buffer, sys.stdout.buffer
        # One line at a time on the loop's own thread: nothing else runs
        # meanwhile, and input faster than masking waits in the pipe.
        while raw := source.readline():
            # A stray byte in a log must not end a long-running pipe.
            line = raw.decode("utf-8", errors="replace")
            if restore:
                # Read for every line, not once at the start: a pipe masking
                # into the same session may have created it or added values
                # since, and a substitute such as a fake name cannot be told
                # from text to know when to look.
                # A session pruned or expired meanwhile ends the restore.
                current = await engine.vault.get(sid)
                if current is None:
                    raise SessionNotFoundError(f"session not found: {session_id}")
                line = (await engine.operator.deanonymize(line, current, context)).text
            else:
                # Read, masked, and saved per line under the session's lock:
                # another writer (a second pipe, the proxy) may have added
                # values since, and a copy kept from the start would give
                # their tokens to new values and overwrite them on save.
                # Saved before written, so a pipe that is killed keeps what
                # it masked.
                async with engine.session_scope(sid, require_existing=True) as session:
                    line = (await engine.transform(line, session=session, persist=False)).text
            out.write(line.encode("utf-8"))
            out.flush()
    finally:
        await close()
