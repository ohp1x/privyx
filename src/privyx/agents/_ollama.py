"""The ``ollama`` type: ``ollama run`` against a local or named Ollama server."""

from __future__ import annotations

from urllib.parse import urlsplit

from privyx.agents._base import Agent, Endpoint, LaunchContext
from privyx.core import ConfigError, PrivyxError

#: The variable naming the server, which the tool and the proxy both read.
HOST = "OLLAMA_HOST"


def ollama_url(host: str) -> str:
    """The server URL an ``OLLAMA_HOST`` value names, read as Ollama reads it.

    No scheme means ``http`` on port 11434; ``http://`` or ``https://`` without
    a port means 80 or 443; no host, or the any-address a server binds to
    (``0.0.0.0``), means ``127.0.0.1``.

    Raises:
        ConfigError: If it is not an address.
    """
    value = host.strip()
    scheme, sep, rest = value.partition("://")
    port = {"http": 80, "https": 443}.get(scheme, 11434) if sep else 11434
    if not sep:
        scheme, rest = "http", value
    try:
        parts = urlsplit(f"{scheme}://{rest}")
        port = parts.port or port
    except ValueError as exc:
        raise ConfigError("OLLAMA_HOST is not a valid address") from exc
    name = parts.hostname or "127.0.0.1"
    name = "127.0.0.1" if name in ("0.0.0.0", "::") else name
    name = f"[{name}]" if ":" in name else name
    return f"{scheme}://{name}:{port}{parts.path.rstrip('/')}"


class OllamaAgent(Agent):
    """Ollama's own client: ``ollama:<model>`` runs ``ollama run <model>``.

    Its server is the one ``OLLAMA_HOST`` names, which the proxy follows, and
    it must answer before the tool starts.
    """

    name = "ollama"

    def expand(self, target: str, args: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        if not target.startswith("ollama:"):
            return None
        model = target.removeprefix("ollama:")
        if not model:
            raise ConfigError("name a model after ollama:, as in ollama:llama3.2")
        return "ollama", ("run", model, *args)

    def endpoint(self, launch: LaunchContext) -> Endpoint | None:
        value = launch.environment.get(HOST)
        return Endpoint(ollama_url(value), f"environment {HOST}") if value else None

    def check(self, upstream: str) -> None:
        """Any HTTP answer will do: a server behind an authenticating proxy may answer a 401."""
        import httpx

        url = upstream.rstrip("/") + "/api/version"
        try:
            httpx.get(url, timeout=3.0)
        except (httpx.TransportError, httpx.InvalidURL) as exc:
            raise PrivyxError(
                f"no Ollama server answers at {upstream} ({type(exc).__name__}); "
                "start one with `ollama serve`, or name another with --upstream or OLLAMA_HOST"
            ) from None
