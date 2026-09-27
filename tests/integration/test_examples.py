"""Every script in ``examples/`` runs.

Nothing else exercises them, and the docs embed them, so one that breaks
(``fastapi_gateway.py`` once did) would go unnoticed and be shown to readers.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import uvicorn

EXAMPLES = sorted((Path(__file__).parents[2] / "examples").glob("*.py"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
async def test_example_runs(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def serve(self: uvicorn.Server, *args: object, **kwargs: object) -> None:
        """Return at once instead of listening until Ctrl-C."""

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await runpy.run_path(str(path))["main"]()
