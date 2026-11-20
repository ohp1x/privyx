"""The example plugins in ``plugins/`` load, pass their kind's suite, and do what the docs show.

Nothing else imports them, and the documentation embeds them, so one that
breaks would go unnoticed and be shown to readers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from privyx.app import Privyx
from privyx.app._build import build_detector
from privyx.app._plugins import load_plugins
from privyx.config.schema import Settings
from privyx.core import Context
from privyx.detectors import Detector
from privyx.testing import DetectorSuite

SHIPPED = Path(__file__).parents[3] / "plugins"


def _load() -> Privyx:
    return load_plugins(Settings(plugins={"paths": [str(SHIPPED)]}))


def _detector(name: str) -> Any:
    return _load().registry(Detector).get(name)


def test_shipped_detectors_register() -> None:
    registry = _load().registry(Detector)
    assert [name for name in registry.names() if not registry.is_builtin(name)] == [
        "iban",
        "license_plate",
    ]


class TestIbanDetector(DetectorSuite):
    component = _detector("iban")
    options = {"countries": ["de", "gb"]}
    samples = {
        "pay DE89 3704 0044 0532 0130 00 EUR or GB82WEST12345698765432": [
            "DE89 3704 0044 0532 0130 00",
            "GB82WEST12345698765432",
        ]
    }


class TestLicensePlateDetector(DetectorSuite):
    component = _detector("license_plate")
    samples = {"parked at B 1234 XYZ": ["B 1234 XYZ"]}


async def test_iban_detector_keeps_only_a_valid_checksum() -> None:
    detector = build_detector({"type": "iban"}, _load())
    text = "DE89 3704 0044 0532 0130 00 EUR, GB82WEST12345698765432, not DE89370400440532013001"
    found = await detector.detect(text, Context())
    assert [(span.entity_type, span.text) for span in found.spans] == [
        ("IBAN", "DE89 3704 0044 0532 0130 00"),
        ("IBAN", "GB82WEST12345698765432"),
    ]


async def test_iban_detector_takes_a_countries_option() -> None:
    detector = build_detector({"type": "iban", "countries": ["nl"]}, _load())
    found = await detector.detect("GB82WEST12345698765432 NL91ABNA0417164300", Context())
    assert [span.text for span in found.spans] == ["NL91ABNA0417164300"]
