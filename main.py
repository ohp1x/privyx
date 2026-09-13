"""Privyx — AI data privacy gateway.

Entry point for the installed package.  The CLI is the primary interface:

    privyx proxy    # start the privacy proxy
    privyx doctor   # run diagnostics
    privyx detect "text"  # test entity detection
"""

from __future__ import annotations

from privyx.cli.main import cli


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
