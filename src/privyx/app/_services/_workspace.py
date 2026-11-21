"""The workspace service: the scan of the working directory, and the checks of its git hooks.

``privyx scan``, ``privyx install-hook`` and ``privyx hook``, and the
``privyx_precommit_check`` tool of ``privyx mcp serve`` call it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from privyx.core import PrivyxError
from privyx.workspace import Added, Found, Installed, Scan

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.token import FormatCodec

#: An ANSI color or style escape, as `git diff --color` writes them.
_COLOR = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Committed:
    """A secret or a Privyx placeholder about to be committed, by file and line.

    ``what`` names a secret by its type only, a placeholder as it is written.
    """

    path: str
    line: int
    what: str


class Workspace:
    def __init__(self, app: Privyx) -> None:
        self._app = app
        self._codec: tuple[FormatCodec, str | None] | None = None

    def codec(self) -> tuple[FormatCodec, str | None]:
        """The configured token codec, and why the default one stands in, if it does.

        A hook must still work in a repository whose config cannot be read.
        """
        if self._codec is None:
            from privyx.app._build import build_codec
            from privyx.token import FormatCodec

            try:
                self._codec = (build_codec(self._app.settings, self._app), None)
            except PrivyxError as exc:
                self._codec = (FormatCodec.default(), str(exc))
        return self._codec

    def check_diff(self, diff: str) -> list[Committed]:
        """The secrets and placeholders on the lines a unified diff adds."""
        from privyx.workspace import added_lines, findings

        # A diff git colored (`color.diff=always`) starts no line with `+` or `@@`.
        diff = _COLOR.sub("", diff)
        codec = self.codec()[0]
        return [
            Committed(path, line, what)
            for path, lines in added_lines(diff).items()
            for line, what in findings(lines, codec)
        ]

    def check_staged(self) -> list[Committed]:
        """The secrets and placeholders the changes staged for the next commit add.

        Raises:
            PrivyxError: If git cannot be run or finds no repository.
        """
        from privyx.workspace import staged_diff

        return self.check_diff(staged_diff())

    def check_message(self, text: str) -> list[Committed]:
        """The secrets and placeholders in a commit message, every line of it."""
        from privyx.workspace import findings

        lines = list(enumerate(text.split("\n"), 1))
        return [
            Committed("commit message", line, what)
            for line, what in findings(lines, self.codec()[0])
        ]

    def install_hooks(self) -> Installed:
        """Install the git hooks in the repository of the working directory.

        Raises:
            PrivyxError: If git cannot be run or finds no repository.
        """
        from privyx.workspace import install_hooks

        return install_hooks()

    def scan(self, root: Path) -> Scan:
        """What ``root`` tells about its people, owners, and secrets, not in its terms file yet.

        Raises:
            ConfigError: If the terms file cannot be read.
        """
        from privyx.workspace import scan

        return scan(root)

    def add(self, found: Scan, items: list[Found]) -> Added:
        """Add ``items`` of a scan to the workspace's terms file, which ``privyx.yaml`` includes.

        Raises:
            ConfigError: If ``privyx.yaml`` cannot be trusted.
        """
        from privyx.workspace import add

        return add(found, items)
