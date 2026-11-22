"""Assemble Privyx in Python: a detector of your own, a route, and the engine.

`python examples/privyx_app.py` masks and restores a text with the engine;
`python examples/privyx_app.py serve` serves the proxy, as `privyx proxy`
does, with the route below beside it.
"""

from __future__ import annotations

import asyncio
import re
import sys

from privyx import Privyx
from privyx.core import Context, Detection
from privyx.detectors import Detector

# Read like `privyx proxy`: the config files, then PRIVYX_* variables, then
# these settings, which win as command-line flags do.  The codename detector
# runs after the built-in regex one.
px = Privyx(detectors={"codename": {}})

CODENAME = re.compile(r"\bproject [A-Z][a-z]+\b")


@px.add
class CodenameDetector(Detector):
    """Masks project codenames, which no built-in pattern knows."""

    name = "codename"

    def detect_sync(self, text: str, context: Context) -> Detection:
        found = Detection()
        for match in CODENAME.finditer(text):
            found.add(match.start(), match.end(), "PROJECT", match.group())
        return found


@px.router.get("/codename/status")
async def status() -> dict[str, str]:
    """Served by the proxy at http://127.0.0.1:8000/codename/status."""
    return {"detector": "codename"}


async def main() -> None:
    async with px.engine() as engine:
        session = await engine.get_or_create_session()

        masked = await engine.transform(
            "project Bluebird ships to dana@acme.example", session=session
        )
        # <PRIVYX_PROJECT_A845F4A8ED736A3F> ships to <PRIVYX_EMAIL_19ECE4788CA5AE6F>
        print(masked.text)

        restored = await engine.restore(f"Noted: {masked.text}", session.session_id)
        print(restored.text)  # Noted: project Bluebird ships to dana@acme.example


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        px.serve()
    else:
        asyncio.run(main())
