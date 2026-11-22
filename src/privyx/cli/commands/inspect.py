"""`privyx inspect` — the names of earlier versions, kept until 0.4.0.

Each command runs its replacement with a warning: `config routes`,
`detect --pattern` or `--entity`, `session show`, and `session token`.
"""

from __future__ import annotations

import click

from privyx.cli._options import config_options, deprecated


@click.group("inspect", hidden=True)
def inspect() -> None:
    """Inspect Privyx internal state (use `config routes`, `detect`, `session`)."""


@inspect.command("routes")
@config_options
@click.pass_context
def routes(ctx: click.Context) -> None:
    """Show the masked request paths (use `privyx config routes`)."""
    from privyx.cli.commands.config import routes as config_routes

    deprecated("`privyx inspect routes`", "`privyx config routes`")
    ctx.invoke(config_routes)


@inspect.command("regex")
@click.argument("pattern")
@config_options
def regex(pattern: str) -> None:
    """Check a regex or an entity's (use `privyx detect --pattern` or `--entity`)."""
    import sys

    from privyx.app import Privyx
    from privyx.cli.commands.detect import check_regex
    from privyx.core import PrivyxError

    try:
        entity = pattern in Privyx().inspection.patterns()
        deprecated(
            "`privyx inspect regex`",
            "`privyx detect --entity NAME`" if entity else "`privyx detect --pattern REGEX`",
        )
        check_regex(None if entity else pattern, pattern if entity else None)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@inspect.command("session")
@click.argument("session_id")
@click.option("--reveal", is_flag=True, help="Show original values in full (sensitive!)")
@config_options
@click.pass_context
def session(ctx: click.Context, session_id: str, reveal: bool) -> None:
    """Show one session's mappings (use `privyx session show`)."""
    from privyx.cli.commands.session import show

    deprecated("`privyx inspect session`", "`privyx session show`")
    ctx.invoke(show, session_id=session_id, reveal=reveal)


@inspect.command("token")
@click.argument("token")
@click.option("--reveal", is_flag=True, help="Show the original value in full (sensitive!)")
@config_options
@click.pass_context
def token(ctx: click.Context, token: str, reveal: bool) -> None:
    """Show what a token stands for (use `privyx session token`)."""
    from privyx.cli.commands.session import token as session_token

    deprecated("`privyx inspect token`", "`privyx session token`")
    ctx.invoke(session_token, token=token, reveal=reveal)
