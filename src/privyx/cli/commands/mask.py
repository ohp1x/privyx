"""`privyx mask` / `privyx unmask` — pseudonymize and restore text off-proxy.

Orchestration only: both commands are thin wrappers over
:meth:`~privyx.engine.PrivacyEngine.transform` and the configured
operator's ``deanonymize``, so what the proxy does to a chat message is exactly
what these do to a file, a JSON document, or a pipe.

The mapping a round trip needs can live in two places: the configured vault
(``--session ID``) or a self-contained map file (``--map FILE``), which needs no
vault and no Privyx deployment at all.  ``--stream`` masks or restores stdin
one line at a time, for a pipeline that never ends, such as ``tail -f``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import click

from privyx.cli._options import config_options, deprecated, drop_stdout, interrupt
from privyx.core import Session

#: Input formats.  ``auto`` looks at the input file's extension.
_FORMATS = ("auto", "text", "json", "jsonl")

#: Errors these commands report as ``Error: ...`` + exit 1 rather than a traceback.
_EXPECTED = (json.JSONDecodeError, OSError)


def _common_options(fn: Callable[..., None]) -> Callable[..., None]:
    """Both commands take the same input/output/session options."""
    options = [
        click.argument("text", nargs=-1, required=False),
        click.option("--stdin", "use_stdin", is_flag=True, help="Read input from stdin"),
        click.option(
            "-i", "--input", "input_path", default=None, help="Input file ('-' for stdin)"
        ),
        click.option(
            "-o", "--output", "output_path", default=None, help="Output file (default: stdout)"
        ),
        click.option(
            "-f",
            "--format",
            "fmt",
            type=click.Choice(_FORMATS),
            default="auto",
            help="Input format; 'auto' guesses from the input file's extension",
        ),
        click.option(
            "--path",
            "paths",
            multiple=True,
            help="Only walk this JSON path prefix, e.g. '$.messages' (repeatable)",
        ),
        click.option("--map", "map_path", default=None, help="Mapping file to read/write"),
        click.option("--session", "session_id", default=None, help="Vault session id"),
        click.option(
            "--stream",
            is_flag=True,
            help="Read stdin and write each line as soon as it is read (needs a vault session).",
        ),
        config_options,
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


@click.command()
@_common_options
def mask(
    text: tuple[str, ...],
    use_stdin: bool,
    input_path: str | None,
    output_path: str | None,
    fmt: str,
    paths: tuple[str, ...],
    map_path: str | None,
    session_id: str | None,
    stream: bool,
) -> None:
    """Replace sensitive values with tokens, reversibly.

    Uses the configured detector, policy, and operator, so the output is what
    the proxy would send upstream. `privyx unmask` puts the originals back,
    given the same --map file or --session id.

    An existing --map file is continued, not overwritten: masking a second
    document against it keeps the tokens the first one was given.

    With --stream, each line of stdin is masked and written as soon as it is
    read, so `tail -f app.log | privyx mask --stream | ...` never waits for
    the end of the input.  A value split across lines, like a PEM private
    key, is not seen whole.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    if stream:
        _stream_only(text, input_path, output_path, map_path, paths)
        _run_stream(session_id, restore=False)
        return
    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx mask [--stdin | -i FILE] <text>")
            sys.exit(1)
        loaded = _load_map(map_path) if map_path and Path(map_path).exists() else None
        app = Privyx()
        if not map_path and not app.sessions.persistent:
            click.echo(
                "Warning: the vault keeps its sessions in this process's memory, so the mapping "
                "is gone when it exits and this output can never be unmasked. Pass --map FILE, "
                "or configure a sqlite file or redis vault.",
                err=True,
            )
        masked = asyncio.run(
            app.masking.mask(
                source,
                fmt=app.masking.format_of(fmt, input_path),
                paths=paths,
                session=loaded,
                session_id=session_id,
            )
        )
        if map_path:
            # The map holds the original values: a new file is owner-only.
            fd = os.open(map_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(masked.session.to_dict(), ensure_ascii=False, indent=2) + "\n")
        click.echo(f"session: {masked.session.session_id}", err=True)
        _write_output(masked.text, output_path)
    except (PrivyxError, *_EXPECTED) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@click.command()
@_common_options
def unmask(
    text: tuple[str, ...],
    use_stdin: bool,
    input_path: str | None,
    output_path: str | None,
    fmt: str,
    paths: tuple[str, ...],
    map_path: str | None,
    session_id: str | None,
    stream: bool,
) -> None:
    """Put the original values back, from a --map file or a --session id.

    Tokens the mapping does not know are left untouched.  With --stream,
    each line of stdin as soon as it is read, given the --session the lines
    were masked with.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    if stream:
        _stream_only(text, input_path, output_path, map_path, paths)
        if not session_id:
            click.echo("Error: unmask --stream needs --session ID", err=True)
            sys.exit(1)
        _run_stream(session_id, restore=True)
        return
    if not map_path and not session_id:
        click.echo("Error: unmask needs --map FILE or --session ID", err=True)
        sys.exit(1)
    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx unmask [--stdin | -i FILE] --map FILE <text>")
            sys.exit(1)
        app = Privyx()
        result = asyncio.run(
            app.masking.unmask(
                source,
                fmt=app.masking.format_of(fmt, input_path),
                paths=paths,
                session=_load_map(map_path) if map_path else None,
                session_id=session_id,
            )
        )
        _write_output(result, output_path)
    except (PrivyxError, *_EXPECTED) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# --------------------------------------------------------------------------
# I/O plumbing — kept out of the coroutines so no blocking file call happens
# inside the event loop.


def _read_input(text: tuple[str, ...], use_stdin: bool, input_path: str | None) -> str | None:
    """Return the input, or ``None`` when no source was given at all.

    ``None`` is distinct from ``""``: an empty file is valid input.
    """
    if use_stdin or input_path == "-":
        return sys.stdin.read()
    if input_path:
        return Path(input_path).read_text(encoding="utf-8")
    return " ".join(text) if text else None


def _write_output(text: str, output_path: str | None) -> None:
    if output_path and output_path != "-":
        Path(output_path).write_text(text, encoding="utf-8")
        return
    # Don't double the newline when the input already ended with one.
    click.echo(text, nl=not text.endswith("\n"))


def _load_map(map_path: str) -> Session:
    return Session.from_dict(json.loads(Path(map_path).read_text(encoding="utf-8")))


@click.command(hidden=True)
@click.option("--session", "session_id", default=None, help="Vault session id")
@click.option("--restore", is_flag=True, help="Put the original values back instead of masking")
@config_options
def pipe(session_id: str | None, restore: bool) -> None:
    """Mask stdin to stdout one line at a time (use `privyx mask --stream`)."""
    deprecated(
        f"`privyx pipe{' --restore' if restore else ''}`",
        f"`privyx {'unmask' if restore else 'mask'} --stream`",
    )
    if restore and not session_id:
        click.echo("Error: --restore needs --session ID", err=True)
        sys.exit(1)
    _run_stream(session_id, restore=restore)


def _stream_only(
    text: tuple[str, ...],
    input_path: str | None,
    output_path: str | None,
    map_path: str | None,
    paths: tuple[str, ...],
) -> None:
    """Refuse the options of a whole document with ``--stream``, which reads stdin to stdout."""
    given = [
        name
        for name, value in (
            ("TEXT", text),
            ("--input", input_path),
            ("--output", output_path),
            ("--map", map_path),
            ("--path", paths),
        )
        if value
    ]
    if given:
        raise click.UsageError(
            f"--stream reads stdin and writes stdout: leave out {', '.join(given)}"
        )


def _run_stream(session_id: str | None, *, restore: bool) -> None:
    """Mask, or restore, stdin to stdout one line at a time."""
    from privyx.core import PrivyxError

    # Ctrl-C raises at once, even in a read or write that blocks: asyncio.run's
    # own handler only cancels the task, which waits for that call to return,
    # so a pipe that gets no more input, or whose reader stopped reading,
    # would never stop.  asyncio.run keeps a handler that is not Python's own.
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        asyncio.run(_stream(session_id, restore))
    except KeyboardInterrupt:
        drop_stdout()
        sys.exit(130)
    except BrokenPipeError:
        drop_stdout()  # the reader went away (`| head`): stop quietly
    except (PrivyxError, OSError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    finally:
        signal.signal(signal.SIGINT, previous)


async def _stream(session_id: str | None, restore: bool) -> None:
    from privyx.app import Privyx
    from privyx.core import Context, SessionNotFoundError

    app = Privyx()
    if not restore and not app.sessions.persistent:
        click.echo(
            "Warning: the vault keeps its sessions in this process's memory, so this output "
            "can never be restored. Configure a sqlite file or redis vault to keep the mapping.",
            err=True,
        )
    async with app.engine() as engine:
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
