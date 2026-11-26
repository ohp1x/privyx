"""The image's defaults, as its ``privyx-env`` script applies them, and where requests then go."""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from privyx import load_config
from privyx.providers import resolve_base_url

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"

# The script reads the environment with GNU sed and grep, as the image has them.
_GNU = subprocess.run(
    ["sh", "-c", r"printf 'a\0' | sed -zn p | grep -qz a"], capture_output=True, check=False
)
pytestmark = pytest.mark.skipif(_GNU.returncode != 0, reason="needs GNU sed and grep")


def _image_env(env: dict[str, str]) -> dict[str, str]:
    """The ``PRIVYX_`` variables once the script has applied its defaults to ``env``."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    script = re.search(r"/app/bin/privyx-env\n(.*?)\nEOF", text, re.S)
    assert script
    done = subprocess.run(
        ["sh", "-c", script[1] + "\nenv -0"],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        check=True,
        timeout=20,
    )
    pairs = (item.split("=", 1) for item in done.stdout.decode().split("\0") if item)
    return {name: value for name, value in pairs if name.startswith("PRIVYX_")}


@pytest.mark.parametrize("pick", ["environment", "config"])
def test_a_provider_picked_by_name_keeps_its_host_under_an_older_compose_default(
    pick: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older docker-compose.yml set this; with the provider's key in .env, the
    # key and the requests went to the OpenAI host.
    given = {"DEFAULT_PRIVYX_UPSTREAM_URL": "https://api.openai.com"}
    if pick == "environment":
        given["PRIVYX_PROVIDER"] = "anthropic"
    else:
        config = tmp_path / "privyx.yaml"
        config.write_text("provider: anthropic\n", encoding="utf-8")
        given["PRIVYX_CONFIG"] = str(config)
    image = _image_env(given)
    assert "PRIVYX_UPSTREAM_URL" not in image

    for name in [name for name in os.environ if name.startswith("PRIVYX_")]:
        monkeypatch.delenv(name)
    for name, value in image.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)

    assert urlsplit(resolve_base_url(load_config())).hostname == "api.anthropic.com"
