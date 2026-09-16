"""`privyx mask` / `privyx unmask` — pseudonymize and restore text off-proxy.

Orchestration only: both commands are thin wrappers over
:meth:`~privyx.core.engine.PrivacyEngine.transform` and the configured
operator's ``deanonymize``, so what the proxy does to a chat message is exactly
what these do to a file, a JSON document, or a pipe.

The mapping a round trip needs can live in two places: the configured vault
(``--session ID``) or a self-contained map file (``--map FILE``), which needs no
vault and no Privyx deployment at all.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click

from privyx.core.session import Session

#: Input formats.  ``auto`` looks at the input file's extension.
_FORMATS = ("auto", "text", "json", "jsonl")

_EXTENSIONS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}

#: Errors these commands report as ``Error: ...`` + exit 1 rather than a traceback.
_EXPECTED = (json.JSONDecodeError, OSError)

#: Applied to one string leaf; async because the engine is.
Transform = Callable[[str], Awaitable[str]]


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
        click.option("--config", "-c", "config_path", default=None, help="Config file path"),
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
    from privyx.core.errors import PrivyxError

    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx mask [--stdin | -i FILE] <text>")
            sys.exit(1)
        loaded = _load_map(map_path) if map_path and Path(map_path).exists() else None
        result, session = asyncio.run(
            _mask_async(
                source,
                _resolve_format(fmt, input_path),
                paths,
                loaded,
                session_id,
                config_path,
                warn_unpersisted=not map_path,
            )
        )
        if map_path:
            Path(map_path).write_text(_dumps(session.to_dict(), indent=2) + "\n", encoding="utf-8")
        click.echo(f"session: {session.session_id}", err=True)
        _write_output(result, output_path)
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
    from privyx.core.errors import PrivyxError

    if not map_path and not session_id:
        click.echo("Error: unmask needs --map FILE or --session ID", err=True)
        sys.exit(1)
    try:
        source = _read_input(text, use_stdin, input_path)
        if source is None:
            click.echo("Usage: privyx unmask [--stdin | -i FILE] --map FILE <text>")
            sys.exit(1)
        result = asyncio.run(
            _unmask_async(
                source,
                _resolve_format(fmt, input_path),
                paths,
                _load_map(map_path) if map_path else None,
                session_id,
                config_path,
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


def _resolve_format(fmt: str, input_path: str | None) -> str:
    if fmt != "auto":
        return fmt
    if input_path and input_path != "-":
        return _EXTENSIONS.get(Path(input_path).suffix.lower(), "text")
    return "text"


def _write_output(text: str, output_path: str | None) -> None:
    if output_path and output_path != "-":
        Path(output_path).write_text(text, encoding="utf-8")
        return
    # Don't double the newline when the input already ended with one.
    click.echo(text, nl=not text.endswith("\n"))


def _load_map(map_path: str) -> Session:
    return Session.from_dict(json.loads(Path(map_path).read_text(encoding="utf-8")))


def _dumps(payload: Any, indent: int | None = None) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=indent)


# --------------------------------------------------------------------------
# Walking the input


async def _apply(source: str, fmt: str, paths: tuple[str, ...], fn: Transform) -> str:
    """Apply ``fn`` to every string the chosen format exposes."""
    if fmt == "text":
        return await fn(source)
    if fmt == "jsonl":
        lines = []
        for line in source.splitlines():
            if not line.strip():
                lines.append(line)
                continue
            lines.append(_dumps(await _apply_json(json.loads(line), paths, fn)))
        return "\n".join(lines)
    return _dumps(await _apply_json(json.loads(source), paths, fn), indent=2)


async def _apply_json(payload: Any, paths: tuple[str, ...], fn: Transform) -> Any:
    """Apply ``fn`` to every string leaf of a JSON document."""
    from privyx.privacy.transform.json import map_strings

    selector = list(paths) or None
    # ponytail: two passes (collect, then substitute) because map_strings takes a
    # sync callable while the engine is async, and the leaves go through fn
    # serially.  Add an async map_strings if either ever costs anything real.
    leaves: list[str] = []

    def collect(leaf: str) -> str:
        leaves.append(leaf)
        return leaf

    map_strings(payload, collect, paths=selector)
    replaced = iter([await fn(leaf) for leaf in leaves])
    return map_strings(payload, lambda _leaf: next(replaced), paths=selector)


# --------------------------------------------------------------------------
# The two pipelines


async def _mask_async(
    source: str,
    fmt: str,
    paths: tuple[str, ...],
    loaded: Session | None,
    session_id: str | None,
    config_path: str | None,
    *,
    warn_unpersisted: bool,
) -> tuple[str, Session]:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    load_plugins(settings)
    if warn_unpersisted and settings.vault.type == "memory":
        click.echo(
            "Warning: vault type is 'memory', so the mapping is gone when this process "
            "exits and this output can never be unmasked. Pass --map FILE, or configure "
            "a sqlite/redis vault.",
            err=True,
        )
    engine, close = await build_engine(settings)
    try:
        session = loaded if loaded is not None else await engine.get_or_create_session(session_id)

        async def transform(leaf: str) -> str:
            return (await engine.transform(leaf, session=session)).text

        return await _apply(source, fmt, paths, transform), session
    finally:
        await close()


async def _unmask_async(
    source: str,
    fmt: str,
    paths: tuple[str, ...],
    loaded: Session | None,
    session_id: str | None,
    config_path: str | None,
) -> str:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_engine
    from privyx.core.context import Context
    from privyx.core.errors import SessionNotFoundError
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    load_plugins(settings)
    engine, close = await build_engine(settings)
    try:
        # Resolve the session once, not once per leaf: a JSON document can hold
        # thousands of strings and every one of them would hit the vault.
        session = loaded if loaded is not None else await engine.vault.get(session_id or "")
        if session is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        context = Context(session_id=session.session_id, vault=engine.vault)

        async def restore(leaf: str) -> str:
            return (await engine.operator.deanonymize(leaf, session, context)).text

        return await _apply(source, fmt, paths, restore)
    finally:
        await close()
