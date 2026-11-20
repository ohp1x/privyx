"""Compare what Privyx does with the golden files next to the tests.

A golden file holds the whole observable result of a test: the launch plan of
each built-in profile, the masked requests and restored replies of each API
format.  A change to it is a change of behavior.  After an intended one,
rewrite the files and review their diff::

    UPDATE_GOLDEN=1 uv run pytest tests/golden
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
#: Read once: a test may empty the environment it runs in.
UPDATE = bool(os.environ.get("UPDATE_GOLDEN"))

Golden = Callable[[str, Any], None]


@pytest.fixture
def golden() -> Golden:
    """Assert that ``value`` equals the golden file ``name``, or rewrite it with UPDATE_GOLDEN."""

    def check(name: str, value: Any) -> None:
        path = HERE / f"{name}.json"
        text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        if UPDATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return
        where = path.relative_to(HERE)
        assert path.exists(), f"{where} is missing; write it with UPDATE_GOLDEN=1"
        expected = json.loads(path.read_text(encoding="utf-8"))
        assert json.loads(text) == expected, (
            f"differs from {where}; if the change is intended, rewrite it with "
            "UPDATE_GOLDEN=1 and review its diff"
        )

    return check
