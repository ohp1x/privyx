"""Fixtures shared by the CLI tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

Seen = list[tuple[str, dict[str, str], str]]


@pytest.fixture
def upstream() -> Iterator[tuple[str, Seen]]:
    """A local upstream answering every POST with a message; records path, headers, body."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: Seen = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["content-length"]))
            headers = {k.lower(): v for k, v in self.headers.items()}
            seen.append((self.path, headers, raw.decode()))
            body = b'{"type": "message", "content": [{"type": "text", "text": "ok"}]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()
