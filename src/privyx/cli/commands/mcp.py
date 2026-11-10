"""`privyx mcp` — Privyx for MCP servers, over stdio.

MCP's stdio transport is JSON-RPC, one message per line, so no MCP library is
needed.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import contextlib
import json
import os
import queue
import re
import signal
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import click

from privyx.cli.commands.mask import _dumps
from privyx.cli.commands.mcp_serve import serve
from privyx.cli.commands.pipe import _drop_stdout

if TYPE_CHECKING:
    from privyx.core.result import Span

#: Values in a JSON-RPC message that are protocol, not content: its own
#: members, the version MCP's initialize negotiates, and the ids notifications
#: point at a request or its progress with, which must match what the other
#: side sent.
_PROTOCOL = frozenset(
    {
        ("jsonrpc",),
        ("id",),
        ("method",),
        ("params", "protocolVersion"),
        ("result", "protocolVersion"),
        ("params", "requestId"),
        ("params", "progressToken"),
        ("params", "_meta", "progressToken"),
    }
)

#: Every member a JSON-RPC message has; a key outside these is content.
_MEMBERS = frozenset({"jsonrpc", "id", "method", "params", "result", "error"})

#: Seconds a stopped server has to exit before it and its group are killed:
#: under the 2 an MCP client gives this process before killing it.
_GRACE = 1.0

#: What ``json.dumps`` escapes in a string (``ensure_ascii=False``).
_ESCAPED = re.compile(r'["\\\x00-\x1f]')

#: Longest line read from the server: asyncio's 64 KiB default is less than
#: one tool result holding a file.
_LINE_LIMIT = 64 * 1024 * 1024

#: Client lines read ahead of the server.
_READ_AHEAD = 64


@click.group()
def mcp() -> None:
    """Use Privyx with MCP servers (stdio)."""


mcp.add_command(serve)


@mcp.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--session", "session_id", default=None, help="Vault session id")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def wrap(session_id: str | None, config_path: str | None, command: tuple[str, ...]) -> None:
    """Run an MCP server, masking what it sends to the client.

    Put it before the server's command in the client's config:
    `privyx mcp wrap -- npx -y some-mcp-server`.  What the server sends is
    masked, so the model sees placeholders; placeholders in what the client
    sends are restored first, so the server works on the real values.
    """
    from privyx.core.errors import PrivyxError

    try:
        code = asyncio.run(_wrap(command, session_id, config_path))
    except KeyboardInterrupt:
        _drop_stdout()
        code = 130
    except BrokenPipeError:
        _drop_stdout()  # the client went away
        code = 0
    except (PrivyxError, OSError) as exc:
        click.echo(f"Error: {exc}", err=True)
        code = 1
    sys.exit(code if code >= 0 else 128 - code)


async def _wrap(command: tuple[str, ...], session_id: str | None, config_path: str | None) -> int:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine
    from privyx.core.context import Context
    from privyx.core.errors import SessionNotFoundError
    from privyx.core.result import Detection
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    load_plugins(settings)
    engine, close = await build_engine(settings)
    try:
        sid = (await engine.get_or_create_session(session_id)).session_id
        click.echo(f"session: {sid}", err=True)
        context = Context(session_id=sid, vault=engine.vault)

        async def mask(line: bytes) -> bytes:
            # Under the session's lock, as `privyx pipe` does: the proxy or a
            # pipe may be writing to the same session.  A session deleted or
            # expired meanwhile ends the run: one made again would hand the
            # client's old tokens to new values.
            async with engine.session_scope(sid, require_existing=True) as session:

                async def one(text: str) -> str:
                    return (await engine.transform(text, session=session, persist=False)).text

                async def found(text: str) -> Detection:
                    return await engine.policy.decide(
                        await engine.detector.detect(text, context), context
                    )

                async def under(key: str, value: str) -> str:
                    # Alone, a value may not say what it is: `{"password":
                    # "hunter2"}` is a secret by its key, and a context word
                    # may be the key.  What is found with the key, whole or in
                    # part, is added to what is found alone, and the value is
                    # masked as it was sent.
                    spans = (await found(value)).spans
                    spans += _value_spans(key, value, (await found(_probe(key, value))).spans)
                    result = await engine.transform(
                        value,
                        session=session,
                        persist=False,
                        detection=Detection(spans).merged(value),  # alone and with the key overlap
                    )
                    return result.text

                # Not JSON: a server logging to stdout.  Masked as text.
                rewritten = await _rewrite(line, one, under)
                return rewritten or (await one(line.decode(errors="replace"))).encode()

        async def restore(line: bytes) -> bytes:
            # The session as saved now: masking has added to it since the start.
            session = await engine.vault.get(sid)
            if session is None:
                raise SessionNotFoundError(f"session not found: {sid}")

            async def one(text: str) -> str:
                return (await engine.operator.deanonymize(text, session, context)).text

            # Not JSON: the server owns the protocol, so it gets it as sent.
            return await _rewrite(line, one) or line

        try:
            server = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                # A group of its own, to stop with it what it starts: a
                # launcher such as npx runs the server as its child.
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise click.ClickException(f"cannot run {command[0]}: {exc}") from exc
        assert server.stdin is not None and server.stdout is not None
        stdin, stdout = server.stdin, server.stdout

        async def to_server() -> None:
            try:
                async for line in _client_lines():
                    stdin.write(await restore(line))
                    await stdin.drain()
                stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server has exited; its output tells the rest

        write = _client_writer()

        async def to_client() -> None:
            try:
                while line := await stdout.readline():
                    await write(await mask(line))
            finally:
                writer.cancel()  # the server is gone: nothing more to send it

        writer = asyncio.create_task(to_server())
        reader = asyncio.create_task(to_client())

        stopping = False

        def stop() -> None:
            nonlocal stopping
            stopping = True
            reader.cancel()  # output the client is not reading is dropped

        # A client stops a server by closing its stdin, then with SIGTERM.
        with contextlib.suppress(NotImplementedError):  # not on Windows
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop)
        try:
            try:
                await asyncio.wait((reader, writer), return_when=asyncio.FIRST_EXCEPTION)
            finally:
                # Also when Ctrl-C cancelled the wait: the reader must be done
                # with stdout before it is drained.
                reader.cancel()
                writer.cancel()
                results = await asyncio.gather(reader, writer, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result
        except BaseException:
            # A message that failed to mask is never sent: stop the server.
            await _stop(server, stdout)
            raise
        return await (_stop if stopping else _finish)(server, stdout)
    finally:
        await close()


async def _client_lines() -> AsyncIterator[bytes]:
    """Stdin's lines, read by a daemon thread so the server's output keeps flowing.

    Not ``asyncio.to_thread``: ``asyncio.run`` waits for that thread, which
    stays blocked on stdin when the server exits first.  A reader of its own,
    not ``sys.stdin.buffer``: a daemon thread holding that one's lock at exit
    makes Python abort.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | OSError] = asyncio.Queue()
    room = threading.Semaphore(_READ_AHEAD)

    def put(item: bytes | OSError) -> None:
        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody reads
            loop.call_soon_threadsafe(queue.put_nowait, item)

    def read() -> None:
        try:
            with open(sys.stdin.fileno(), "rb", closefd=False) as source:
                for line in source:
                    room.acquire()
                    put(line)
        except OSError as exc:
            put(exc)
        else:
            put(b"")

    threading.Thread(target=read, daemon=True).start()
    while item := await queue.get():
        if isinstance(item, OSError):
            raise item
        room.release()
        yield item


async def _stop(server: asyncio.subprocess.Process, stdout: asyncio.StreamReader) -> int:
    """Stop the server and what it started (its process group); its exit code.

    SIGTERM first, SIGKILL when that has not ended it within ``_GRACE``.
    """
    _signal(server, kill=False)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _GRACE
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(_finish(server, stdout), _GRACE)
    # The group, not just the server: what it started may outlive it, with no
    # hold on its stdout to show for it.
    while _alive(server) and loop.time() < deadline:  # noqa: ASYNC110 - nothing to wait on
        await asyncio.sleep(0.05)
    if _alive(server):
        _signal(server, kill=True)
    return await _finish(server, stdout)


def _alive(server: asyncio.subprocess.Process) -> bool:
    """Whether the server, or on POSIX anything left in its group, still runs."""
    if os.name != "posix":
        return server.returncode is None
    try:
        os.killpg(server.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _signal(server: asyncio.subprocess.Process, *, kill: bool) -> None:
    with contextlib.suppress(ProcessLookupError):
        if os.name != "posix":  # no process groups, and no SIGKILL to name
            server.kill() if kill else server.terminate()
        else:
            os.killpg(server.pid, signal.SIGKILL if kill else signal.SIGTERM)


async def _finish(server: asyncio.subprocess.Process, stdout: asyncio.StreamReader) -> int:
    """The server's exit code, once it has exited.

    What it wrote that nobody read is read and dropped first: ``wait`` also
    waits for its stdout to close, and a full buffer is no longer read from.
    """
    while await stdout.read(1 << 16):
        pass
    return await server.wait()


def _client_writer() -> Callable[[bytes], Awaitable[None]]:
    """A write to stdout that waits off the loop.

    A client that stops reading fills the pipe; a write on the loop would then
    hold up the other direction and the SIGTERM handler.  ``os.write`` from a
    daemon thread, as stdin is read: no file object whose lock it could hold
    at exit.
    """
    loop = asyncio.get_running_loop()
    jobs: queue.SimpleQueue[tuple[bytes, asyncio.Future[OSError | None]]] = queue.SimpleQueue()
    fd = sys.stdout.fileno()

    def settle(done: asyncio.Future[OSError | None], error: OSError | None) -> None:
        if not done.done():  # not cancelled meanwhile
            done.set_result(error)

    def run() -> None:
        while True:
            data, done = jobs.get()
            error = None
            try:
                while data:
                    data = data[os.write(fd, data) :]
            except OSError as exc:
                error = exc
            with contextlib.suppress(RuntimeError):  # the loop has closed
                loop.call_soon_threadsafe(settle, done, error)

    threading.Thread(target=run, daemon=True).start()

    async def write(data: bytes) -> None:
        done: asyncio.Future[OSError | None] = loop.create_future()
        jobs.put((data, done))
        if error := await done:
            raise error

    return write


def _probe(key: str, value: str) -> str:
    """``value`` with its key in front, as text: ``key: "value"``, JSON-escaped."""
    return f"{key}: {json.dumps(value, ensure_ascii=False)}"


def _value_spans(key: str, value: str, spans: list[Span]) -> list[Span]:
    """``spans`` found in ``_probe(key, value)``, cut to the value, at its offsets.

    A span ending inside an escape (``\\n`` for a newline) takes the character.
    """
    from privyx.core.result import Span

    start = len(key) + 3
    end = start + len(json.dumps(value, ensure_ascii=False)) - 2
    # Each character JSON escaped: where its escape starts, how long it is,
    # and how many characters escapes added before it.
    at: list[int] = []
    size: list[int] = []
    before: list[int] = []
    added = 0
    for match in _ESCAPED.finditer(value):
        length = len(json.dumps(match.group(), ensure_ascii=False)) - 2
        at.append(match.start() + added)
        size.append(length)
        before.append(added)
        added += length - 1

    def raw(offset: int) -> int:
        k = bisect.bisect_right(at, offset) - 1
        if k < 0:
            return offset
        if offset < at[k] + size[k]:
            return at[k] - before[k]
        return offset - before[k] - size[k] + 1

    out = []
    for span in spans:
        low, high = max(span.start, start) - start, min(span.end, end) - start
        if low < high:
            first, last = raw(low), raw(high - 1) + 1
            out.append(Span(first, last, span.entity_type, value[first:last]))
    return out


def _binary(parent: dict[str, Any], key: str, value: Any) -> bool:
    """Whether ``value`` is an MCP base64 payload: image or audio ``data``, a ``blob``.

    Passed on unread, as the proxy does: a match inside base64 is chance, and
    masking it only breaks the payload.  A value there that is not base64 is
    text, and masked.
    """
    if not isinstance(value, str):
        return False
    if key == "data":
        if parent.get("type") not in ("image", "audio"):
            return False
    elif key != "blob" or "uri" not in parent:
        return False
    try:
        base64.b64decode(value, validate=True)
    except ValueError:
        return False
    return True


def _text(value: Any) -> str | None:
    """A string, or a number as text (a card number may come as one); else ``None``."""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _back(value: Any, text: str, changed: str) -> Any:
    """``changed`` in place of ``value``; a number stays one unless it was masked.

    A masked number becomes its token, a string, and is restored as a string.
    """
    return changed if isinstance(value, str) or changed != text else value


async def _rewrite(
    line: bytes,
    fn: Callable[[str], Awaitable[str]],
    under: Callable[[str, str], Awaitable[str]] | None = None,
) -> bytes | None:
    """``line`` with ``fn`` applied to its content; ``None`` when it is not JSON.

    ``under(key, value)``, when given, stands in for ``fn`` on a string or
    number under a key, to see it with that key.

    Every string but the protocol members is content, object keys included (a
    tool's structured result may key on an email), so a server that prints
    some other JSON to stdout is masked too.  What goes on is the message as
    parsed and checked, never ``line`` itself: a parser drops what it does not
    keep, such as the first of two duplicate keys, unchecked.
    """
    from privyx.core.errors import OperatorError

    try:
        message = json.loads(line)
    except ValueError:
        return None

    async def strings(value: Any, path: tuple[str, ...] | None, owner: str | None = None) -> Any:
        """``path`` is where ``value`` sits in a JSON-RPC message, or ``None``;
        ``owner`` the key it is under, also from inside a list
        (``{"passwords": ["..."]}``)."""
        if path in _PROTOCOL:
            return value
        if (text := _text(value)) is not None:
            if under is not None and owner is not None:
                return _back(value, text, await under(owner, text))
            return _back(value, text, await fn(text))
        if isinstance(value, list):
            return [await strings(item, None, owner) for item in value]
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if not _binary(value, key, item):
                    item = await strings(item, None if path is None else (*path, key), key)
                new = key if path == () and key in _MEMBERS else await fn(key)
                if new in out:
                    # Two keys made one (`redact` gives every email the same
                    # text): one member would be dropped without a word.
                    raise OperatorError(f"two keys of one object both became {new!r}")
                out[new] = item
            return out
        return value

    async def message_content(value: Any) -> Any:
        if isinstance(value, list):  # a batch
            return [await message_content(item) for item in value]
        # Some other JSON is content through and through.
        return await strings(value, () if isinstance(value, dict) and "jsonrpc" in value else None)

    changed = await message_content(message)
    return (_dumps(changed) + "\n").encode()
