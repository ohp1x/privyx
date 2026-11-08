"""GenericProvider: where each client path is posted."""

from __future__ import annotations

import pytest

from privyx.providers.generic import GenericProvider

BASE = "https://api.example.com"


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        # Chat and every other path post to the configured endpoint.
        (f"{BASE}/v1/chat/completions", "/v1/chat/completions", f"{BASE}/v1/chat/completions"),
        (f"{BASE}/v1/chat/completions", "/v1/messages/count_tokens", f"{BASE}/v1/chat/completions"),
        (f"{BASE}/v1/embeddings", "/v1/chat/completions", f"{BASE}/v1/embeddings"),
        (f"{BASE}/v1", "/v1/chat/completions", f"{BASE}/v1"),
        # Embeddings and legacy completions go beside it.
        (f"{BASE}/v1/chat/completions", "/v1/embeddings", f"{BASE}/v1/embeddings"),
        (f"{BASE}/v1/chat/completions", "/completions", f"{BASE}/v1/completions"),
        (f"{BASE}/v1/messages", "/embeddings", f"{BASE}/v1/embeddings"),
        (f"{BASE}/v1", "/v1/embeddings", f"{BASE}/v1/embeddings"),
        (f"{BASE}/v1/", "/v1/embeddings", f"{BASE}/v1/embeddings"),
        (f"{BASE}/v1/completions", "/v1/embeddings", f"{BASE}/v1/embeddings"),
        (
            f"{BASE}/openai/deployments/m/chat/completions?api-version=1",
            "/v1/embeddings",
            f"{BASE}/openai/deployments/m/embeddings?api-version=1",
        ),
    ],
)
def test_resolve_url(base_url: str, path: str, expected: str) -> None:
    assert GenericProvider(base_url)._resolve_url(path) == expected


def test_no_path_posts_to_the_base_url() -> None:
    assert GenericProvider(f"{BASE}/v1/chat/completions")._resolve_url(None) == (
        f"{BASE}/v1/chat/completions"
    )
