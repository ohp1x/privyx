"""Unit tests for transparent-proxy header filtering."""

from __future__ import annotations

from privyx.proxy.headers import filter_request_headers, filter_response_headers


def test_strips_hop_by_hop_keeps_the_rest() -> None:
    out = filter_request_headers(
        {
            "Host": "privyx.local",
            "Content-Length": "5",
            "Connection": "keep-alive",
            "Authorization": "Bearer k",
            "X-Custom": "v",
        }
    )
    assert "host" not in out
    assert "content-length" not in out
    assert "connection" not in out
    assert out["authorization"] == "Bearer k"
    assert out["x-custom"] == "v"


def test_strips_accept_encoding_and_internal_session_header() -> None:
    out = filter_request_headers({"Accept-Encoding": "gzip", "X-Privyx-Session": "s"})
    assert "accept-encoding" not in out
    assert "x-privyx-session" not in out


def test_drops_client_auth_when_disabled() -> None:
    out = filter_request_headers(
        {"Authorization": "Bearer k", "x-api-key": "kk"}, forward_client_auth=False
    )
    assert "authorization" not in out
    assert "x-api-key" not in out


def test_api_key_override_openai_uses_bearer() -> None:
    out = filter_request_headers(
        {"Authorization": "Bearer client"}, schema="openai", api_key="server"
    )
    assert out["authorization"] == "Bearer server"


def test_api_key_override_anthropic_uses_x_api_key() -> None:
    out = filter_request_headers(
        {"Authorization": "Bearer client"}, schema="anthropic", api_key="server"
    )
    assert out["x-api-key"] == "server"
    assert "authorization" not in out


def test_anthropic_version_added_when_missing_and_preserved_otherwise() -> None:
    assert filter_request_headers({}, schema="anthropic")["anthropic-version"] == "2023-06-01"
    kept = filter_request_headers({"anthropic-version": "2024-01-01"}, schema="anthropic")
    assert kept["anthropic-version"] == "2024-01-01"


def test_extra_headers_merged_last() -> None:
    out = filter_request_headers({}, extra={"X-Extra": "1"})
    assert out["x-extra"] == "1"


def test_response_headers_drop_encoding_length_and_hop_by_hop() -> None:
    out = filter_response_headers(
        {
            "Content-Encoding": "gzip",
            "Content-Length": "10",
            "Transfer-Encoding": "chunked",
            "X-Request-Id": "r",
        }
    )
    assert "content-encoding" not in out
    assert "content-length" not in out
    assert "transfer-encoding" not in out
    assert out["x-request-id"] == "r"
