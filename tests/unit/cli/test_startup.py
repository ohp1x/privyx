"""`privyx --help` and `--version` import none of the libraries the commands use."""

from __future__ import annotations

import subprocess
import sys

import pytest

HEAVY = ("pydantic", "httpx", "fastapi", "rich", "uvicorn", "yaml")

_RUN = """
import sys
from privyx.cli.main import cli
try:
    cli.main(sys.argv[1:], prog_name="privyx", standalone_mode=False)
except SystemExit:
    pass
print(",".join(sorted(m for m in {heavy!r} if m in sys.modules)))
"""


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["config", "--help"]])
def test_help_and_version_import_no_heavy_library(args: list[str]) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _RUN.format(heavy=HEAVY), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.splitlines()[-1] == ""


def test_importing_privyx_imports_no_heavy_library() -> None:
    code = "import sys, privyx; print(','.join(m for m in {!r} if m in sys.modules))"
    done = subprocess.run(
        [sys.executable, "-c", code.format(HEAVY)], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == ""


def test_the_public_names_are_imported_on_first_use() -> None:
    import privyx

    assert privyx.__version__
    assert privyx.Privyx.__name__ == "Privyx"
    assert privyx.Settings.__name__ == "Settings"
    assert issubclass(privyx.ConfigError, privyx.PrivyxError)
    with pytest.raises(AttributeError):
        privyx.nothing  # noqa: B018
