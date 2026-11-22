"""`privyx mcp` — Privyx for MCP servers, over stdio."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import click

from privyx.cli._options import config_options, drop_stdout, interrupt


@click.group()
def mcp() -> None:
    """Use Privyx with MCP servers (stdio)."""


@mcp.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--session", "session_id", default=None, help="Vault session id")
@config_options
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def wrap(session_id: str | None, command: tuple[str, ...]) -> None:
    """Run an MCP server, masking what it sends to the client.

    Put it before the server's command in the client's config:
    `privyx mcp wrap -- npx -y some-mcp-server`.  What the server sends is
    masked, so the model sees placeholders; placeholders in what the client
    sends are restored first, so the server works on the real values.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.mcp import wrap as wrap_server

    try:
        code = asyncio.run(wrap_server(Privyx(), command, session_id))
    except KeyboardInterrupt:
        drop_stdout()
        code = 130
    except BrokenPipeError:
        drop_stdout()  # the client went away
        code = 0
    except (PrivyxError, OSError) as exc:
        click.echo(f"Error: {exc}", err=True)
        code = 1
    sys.exit(code if code >= 0 else 128 - code)


@mcp.command()
@config_options
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Directory the file tool may read and write in (default: the current one)",
)
def serve(root: Path) -> None:
    """Serve Privyx tools over MCP stdio."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError
    from privyx.mcp import serve as serve_tools

    # Ctrl-C stops at once, as in `privyx mask --stream`: the read from stdin
    # blocks, and asyncio.run's own handler waits for it to return.
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        asyncio.run(serve_tools(Privyx(), root.resolve()))
    except KeyboardInterrupt:
        drop_stdout()
        sys.exit(130)
    except BrokenPipeError:
        drop_stdout()  # the client went away
    except (PrivyxError, OSError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    finally:
        signal.signal(signal.SIGINT, previous)
