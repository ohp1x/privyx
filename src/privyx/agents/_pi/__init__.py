"""The ``pi`` type: pi, through an extension that sends each request to the proxy."""

from __future__ import annotations

from pathlib import Path

from privyx.agents._base import Agent, Endpoint, LaunchContext

#: pi's subcommands, which it reads only as the first argument.
PI_COMMANDS = frozenset(
    {"auth", "config", "install", "list", "mcp", "remove", "uninstall", "update"}
)

#: The extension pi loads: it reads PRIVYX_PI_BASE_URL, which pi itself does not.
EXTENSION = Path(__file__).with_name("extension.js")


class PiAgent(Agent):
    """pi: its extension routes every provider's requests through the proxy.

    Each request names the base URL of its model's provider, with the run's
    token, unless one upstream is chosen for all of them.
    """

    name = "pi"
    upstream_per_request = "PRIVYX_PI_TOKEN"

    def argv(self, base_url: str, argv: list[str], endpoint: Endpoint | None) -> list[str]:
        # A subcommand calls no model, and before it the extension's -e would
        # turn it into a prompt.
        if argv[:1] and argv[0] in PI_COMMANDS:
            return argv
        return ["-e", str(EXTENSION), *argv]

    def env(self, launch: LaunchContext) -> dict[str, str]:
        # One upstream for every request: the extension sends them all as this provider's.
        return {} if launch.per_request else {"PRIVYX_PI_PROVIDER": launch.provider}
