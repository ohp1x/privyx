"""The ``env`` type: a tool that reads its base URL from environment variables."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator

from privyx.agents._base import Agent, Endpoint, LaunchContext
from privyx.config.defaults import DEFAULT_AGENTS

#: For a command with no profile: the base-URL variables most tools read, by
#: provider type, and the path the proxy URL ends in for them.
FALLBACK_ENV: dict[str, tuple[tuple[str, ...], str]] = {
    "openai": (("OPENAI_BASE_URL", "OPENAI_API_BASE"), "/v1"),
    "anthropic": (("ANTHROPIC_BASE_URL",), ""),
    "google": (("GOOGLE_GEMINI_BASE_URL",), ""),
}


class EnvAgent(Agent):
    """A tool pointed at the proxy by ``env_vars`` alone.

    Its own endpoint is the first of ``endpoint_env`` that is set.  A
    multi-provider tool, whose destination cannot be told without reading its
    configuration, names none.
    """

    name = "env"

    class Options(Agent.Options):
        #: Variables that may hold the tool's own endpoint, in order; empty
        #: means those of the built-in profile named like the command's file.
        endpoint_env: list[str] = []

        @field_validator("endpoint_env")
        @classmethod
        def _names(cls, values: list[str]) -> list[str]:
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in values):
                raise ValueError("must contain environment variable names")
            return list(dict.fromkeys(values))

    options: Options

    def endpoint(self, launch: LaunchContext) -> Endpoint | None:
        for key in self.endpoint_env(launch.command):
            if value := launch.environment.get(key):
                return Endpoint(value, f"environment {key}")
        return None

    def endpoint_env(self, command: str) -> list[str]:
        """``endpoint_env``, or that of the built-in profile named like ``command``'s file.

        A plain ``command: aider``, with no ``extends``, relies on the latter.
        """
        if self.options.endpoint_env:
            return self.options.endpoint_env
        builtin = DEFAULT_AGENTS.get(Path(command).name, {}).get("endpoint_env", [])
        return list(builtin) if isinstance(builtin, list) else []
