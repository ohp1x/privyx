"""``decode: true``: values split into strings or encoded are still found."""

from __future__ import annotations

import base64
import time
import urllib.parse
from unittest import mock

import pytest

from privyx.core.context import Context
from privyx.privacy.detector import decode
from privyx.privacy.detector.yaml import build_detector

KEY = "sk-" + "proj-Zq7Xw2Lp9Rt4Vb6Nm1Kc8Hd3"
EMAIL = "ana@example.com"


def _found(text: str, decode: bool = True) -> list[tuple[str, str]]:
    detector = build_detector({"type": "regex", "cache": False, "decode": decode})
    detection = detector.detect_sync(text, Context())  # type: ignore[attr-defined]
    return [(s.entity_type, s.text) for s in detection.merged(text).spans]


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


@pytest.mark.parametrize(
    "hidden",
    [
        " + ".join(repr(char) for char in KEY),  # 's' + 'k' + '-' + …
        f"'{KEY[:2]}'.'{KEY[2:]}'",  # PHP
        f'"{KEY[:4]}" .. "{KEY[4:]}"',  # Lua
        "chr(115) + chr(107) + '" + KEY[2:] + "'",
        _b64(KEY),
        base64.urlsafe_b64encode(KEY.encode()).decode().rstrip("="),
        _b64(_b64(KEY)),  # two levels deep
        KEY.encode().hex(),
    ],
)
def test_split_or_encoded_key_is_masked_whole(hidden: str) -> None:
    text = f"run(key={hidden})"
    assert _found(text, decode=False) == []
    assert _found(text) == [("API_KEY", hidden)]


def test_url_encoding_masks_only_the_value() -> None:
    escaped = urllib.parse.quote(EMAIL)
    text = f"GET https://app.example/?email={escaped}&page=2"
    assert _found(text) == [("EMAIL", escaped)]


@pytest.mark.parametrize(
    "text",
    [
        "x = 'a' + 'b' + 'c'",
        "commit 3f2a9b1c4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "name internationalization_settings_v2",
        "url https://example.com/search?q=hello%20world",
        _b64("just some text with nothing in it"),
        base64.b64encode(bytes(range(256))).decode(),  # binary, not text
    ],
)
def test_nothing_hidden_means_nothing_masked(text: str) -> None:
    assert _found(text) == []


def test_three_levels_deep_is_past_the_limit() -> None:
    assert _found(_b64(_b64(_b64(KEY)))) == []


def test_decode_stays_fast_on_large_blobs() -> None:
    # 256 KiB of Base64-looking text: blobs past 64 KiB are not decoded at all.
    text = " ".join(["A" * 65_000, _b64("x" * 40_000), "%41" * 30_000])
    start = time.perf_counter()
    _found(text)
    assert time.perf_counter() - start < 1.0


def test_budget_is_charged_before_decoding() -> None:
    # 50 blobs of 64,000 characters, each decoding to a 48,000-character blob
    # scanned one level deeper: together they would decode 5,600,000.
    blob = _b64("x" * 48_000)
    text = " ".join([blob] * 50)
    with mock.patch("privyx.privacy.detector.decode._decoded", wraps=decode._decoded) as spy:
        _found(text)
    decoded = sum(len(call.args[0]) for call in spy.call_args_list)
    assert 0 < decoded <= decode.MAX_DECODED
