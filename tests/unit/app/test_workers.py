"""``server.workers``: the proxy served from several processes, each building the app again."""

from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
from typing import Any

import pytest

from privyx.app import Privyx
from privyx.core import ConfigError
from privyx.events import AppStarting


@pytest.fixture
def sqlite(tmp_path: Path) -> dict[str, Any]:
    return {"type": "sqlite", "dsn": f"sqlite+aiosqlite:///{tmp_path / 'privyx.db'}"}


def test_a_vault_of_one_process_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", _never)
    with pytest.raises(ConfigError, match="needs a vault the processes share"):
        Privyx(server={"workers": 2}, features={"audit": False}).serve()


def test_a_feature_with_state_in_its_instance_is_refused(
    monkeypatch: pytest.MonkeyPatch, sqlite: dict[str, Any]
) -> None:
    monkeypatch.setattr("uvicorn.run", _never)
    app = Privyx(
        server={"workers": 2},
        vault=sqlite,
        features={"audit": False, "budget": {"max_turns": 3}},
    )
    with pytest.raises(ConfigError, match="feature 'budget' keeps its state in one process"):
        app.serve()


def test_what_code_added_cannot_reach_the_workers(
    monkeypatch: pytest.MonkeyPatch, sqlite: dict[str, Any]
) -> None:
    monkeypatch.setattr("uvicorn.run", _never)
    app = Privyx(server={"workers": 2}, vault=sqlite, features={"audit": False})
    app.on(AppStarting)(lambda event: None)  # a listener of the app's own

    with pytest.raises(ConfigError, match="without what code added to the app"):
        app.serve()
    with pytest.raises(ConfigError, match="--reload serves with one worker"):
        app.serve(watch=[Path("privyx.yaml")])


def test_each_worker_builds_the_app_the_parent_described(
    monkeypatch: pytest.MonkeyPatch, sqlite: dict[str, Any], tmp_path: Path
) -> None:
    from privyx.app import _serve
    from privyx.config import CHOSEN_PRESETS

    config = tmp_path / "c.yaml"
    config.write_text("presets:\n  quiet:\n    log:\n      level: warning\n", encoding="utf-8")
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: calls.append((app, options)))
    monkeypatch.setenv("PRIVYX_WORKER", "{}")  # restored when the test ends
    overrides = {"server": {"workers": 3, "port": 0}, "vault": sqlite, "features": {"audit": False}}
    token = CHOSEN_PRESETS.set(("quiet",))
    try:
        Privyx(config, **overrides).serve()
    finally:
        CHOSEN_PRESETS.reset(token)

    [(target, options)] = calls
    assert target == "privyx.app._serve:worker"
    assert (options["factory"], options["workers"]) == (True, 3)
    spec = json.loads(os.environ["PRIVYX_WORKER"])
    assert spec == {"config": str(config), "overrides": overrides, "presets": ["quiet"]}
    built: list[Privyx] = []
    monkeypatch.setattr(Privyx, "asgi", lambda self: built.append(self) or "asgi")
    # A worker process chooses the presets for good: here, in a copy of the context.
    assert contextvars.copy_context().run(_serve.worker) == "asgi"
    assert built[0].settings.vault.type == "sqlite"
    assert built[0].settings.log.level == "warning"  # the preset the parent chose


def _never(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("served")
