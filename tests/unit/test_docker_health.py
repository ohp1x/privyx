"""The image's healthcheck script, run against a local server."""

import datetime as dt
import re
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"


def _health(port: int) -> int:
    """The exit code of the script's Python part, with the port in the environment."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    script = re.search(r"/app/bin/privyx-health\n.*?exec python -c '(.*?)'\nEOF", text, re.S)
    assert script
    env = {"PRIVYX_PORT": str(port)}
    done = subprocess.run(
        [sys.executable, "-c", script[1]], env=env, capture_output=True, timeout=20, check=False
    )
    return done.returncode


def _certificate(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate that names no loopback address."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "privyx.example.com")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


@contextmanager
def _server(status: int, tls: tuple[Path, Path] | None) -> Iterator[int]:
    """Serve ``status`` on a free port, over TLS when given a certificate."""

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("use_tls", [False, True], ids=["http", "https"])
@pytest.mark.parametrize(("status", "code"), [(200, 0), (503, 1)])
def test_health_follows_the_server(use_tls: bool, status: int, code: int, tmp_path: Path) -> None:
    tls = _certificate(tmp_path) if use_tls else None
    with _server(status, tls) as port:
        assert _health(port) == code


def test_health_fails_with_no_server() -> None:
    with _server(200, None) as port:
        pass
    assert _health(port) == 1
