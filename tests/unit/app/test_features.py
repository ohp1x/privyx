"""``app.features``: which features the settings switch on, switching them, ``privyx features``."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.app import Privyx
from privyx.cli.main import cli
from privyx.config.schema import Settings
from privyx.core import ConfigError
from privyx.events import RequestMasked


def test_audit_and_metrics_are_on_and_the_old_keys_switch_the_rest() -> None:
    features = Privyx().features
    switched = Settings(
        audit={"enabled": False, "path": "/tmp/x.log"},
        tracing={"enabled": True},
        proxy={"directive": True, "cache_control": True},
    )

    assert features.configured(Settings()) == {
        "audit": {"path": "~/.privyx/audit.log"},
        "metrics": {},
    }
    assert features.configured(switched) == {
        "metrics": {},
        "tracing": {},
        "directive": {},
        "cache_control": {},
    }


async def test_a_feature_switched_on_while_running_listens_from_the_next_event() -> None:
    app = Privyx()
    async with app.features.running(Settings(audit={"enabled": False})):
        assert not app._events.wants(RequestMasked)
        directive = await app.features.enable("directive")
        assert app._events.listeners(RequestMasked) == (directive.add,)
        await app.features.disable("directive")
        assert not app._events.wants(RequestMasked)
        assert app.features.get("directive") is None
    assert app.features.get("metrics") is None  # off again once the app stops


async def test_an_unknown_feature_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="did you mean 'directive'"):
        await Privyx().features.enable("directiv")


def test_privyx_features_list_shows_each_feature_and_what_it_listens_to(tmp_path: Path) -> None:
    config = tmp_path / "privyx.yaml"
    config.write_text("proxy:\n  directive: true\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["features", "list", "--events", "-c", str(config)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "audit          on"
    assert "directive      on" in lines
    assert "budget         off" in lines
    at = lines.index("directive      on")
    assert lines[at + 1] == "  request.masked (priority 0)"


async def test_capture_events_sees_what_the_app_emits_while_it_runs() -> None:
    from privyx.events import AuditEvent, SessionCreated
    from privyx.testing import capture_events

    app = Privyx()
    async with app.features.running(Settings(audit={"enabled": False})) as audit:
        with capture_events(app, AuditEvent) as seen:
            audit.session_created("ses_1")
        audit.session_created("ses_2")  # after the block: not seen

    assert [(type(e), e.session_id) for e in seen] == [(SessionCreated, "ses_1")]
    assert not app._events.wants(SessionCreated)  # nothing else listens once it is off
