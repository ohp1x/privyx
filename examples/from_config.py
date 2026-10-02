"""Build the engine from settings, the way `privyx proxy` does.

Instead of wiring a detector, a policy, an operator, and a vault by hand, load
the same configuration the CLI reads and let Privyx assemble them.
"""

from __future__ import annotations

import asyncio

from privyx.config.loader import load_config
from privyx.core.builder import build_engine


async def main() -> None:
    # The same layers as the CLI: built-in defaults, a YAML file (pass its
    # path as the first argument), PRIVYX_* environment variables, then `extra`.
    settings = load_config(extra={"detector": {"terms": {"PROJECT": ["bluebird"]}}})
    engine, close = await build_engine(settings)
    try:
        session = await engine.get_or_create_session()

        masked = await engine.transform("bluebird ships to dana@acme.example", session=session)
        print(masked.text)  # <PRIVYX_PROJECT_1> ships to <PRIVYX_EMAIL_2>

        reply = f"Noted: {masked.text}"  # stands in for a model's answer
        restored = await engine.restore(reply, session.session_id)
        print(restored.text)  # Noted: bluebird ships to dana@acme.example
    finally:
        await close()  # releases the vault


if __name__ == "__main__":
    asyncio.run(main())
