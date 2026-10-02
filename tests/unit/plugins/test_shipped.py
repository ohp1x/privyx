"""The example plugins in ``plugins/`` load and do what the docs show.

Nothing else imports them, and the documentation embeds them, so one that
breaks would go unnoticed and be shown to readers.
"""

from __future__ import annotations

from pathlib import Path

from privyx.config.schema import Settings
from privyx.core.context import Context
from privyx.plugins.loader import load_plugins
from privyx.plugins.registry import PLUGINS
from privyx.privacy.detector.yaml import build_detector

SHIPPED = Path(__file__).parents[3] / "plugins"


def _load() -> None:
    load_plugins(Settings(plugins={"paths": [str(SHIPPED)]}))


def test_shipped_detectors_register() -> None:
    _load()
    assert PLUGINS.detectors.names() == ["iban", "license_plate"]


async def test_iban_detector_keeps_only_a_valid_checksum() -> None:
    _load()
    detector = build_detector({"type": "iban"})
    text = "DE89 3704 0044 0532 0130 00 EUR, GB82WEST12345698765432, not DE89370400440532013001"
    found = await detector.detect(text, Context())
    assert [(span.entity_type, span.text) for span in found.spans] == [
        ("IBAN", "DE89 3704 0044 0532 0130 00"),
        ("IBAN", "GB82WEST12345698765432"),
    ]


async def test_iban_detector_takes_a_countries_option() -> None:
    _load()
    detector = build_detector({"type": "iban", "countries": ["nl"]})
    found = await detector.detect("GB82WEST12345698765432 NL91ABNA0417164300", Context())
    assert [span.text for span in found.spans] == ["NL91ABNA0417164300"]
