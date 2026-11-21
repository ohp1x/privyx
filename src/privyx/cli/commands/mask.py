"""`privyx mask` / `privyx unmask` — pseudonymize and restore text off-proxy.

Orchestration only: both commands are thin wrappers over
:meth:`~privyx.engine.PrivacyEngine.transform` and the configured
operator's ``deanonymize``, so what the proxy does to a chat message is exactly
what these do to a file, a JSON document, or a pipe.

The mapping a round trip needs can live in two places: the configured vault
(``--session ID``) or a self-contained map file (``--map FILE``), which needs no
vault and no Privyx deployment at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import click

from privyx.cli.config import config_options
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
    config_path: str | None,
) -> None:
    """Replace sensitive values with tokens, reversibly.

    Uses the configured detector, policy, and operator, so the output is what
    the proxy would send upstream. `privyx unmask` puts the originals back,
    given the same --map file or --session id.

    An existing --map file is continued, not overwritten: masking a second
    document against it keeps the tokens the first one was given.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx mask [--stdin | -i FILE] <text>")
            sys.exit(1)
        loaded = _load_map(map_path) if map_path and Path(map_path).exists() else None
        app = Privyx(config_path)
        if not map_path and app.settings.vault.type == "memory":
            click.echo(
                "Warning: vault type is 'memory', so the mapping is gone when this process "
                "exits and this output can never be unmasked. Pass --map FILE, or configure "
                "a sqlite/redis vault.",
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
    config_path: str | None,
) -> None:
    """Put the original values back, from a --map file or a --session id.

    Tokens the mapping does not know are left untouched.
    """
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    if not map_path and not session_id:
        click.echo("Error: unmask needs --map FILE or --session ID", err=True)
        sys.exit(1)
    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx unmask [--stdin | -i FILE] --map FILE <text>")
            sys.exit(1)
        app = Privyx(config_path)
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
