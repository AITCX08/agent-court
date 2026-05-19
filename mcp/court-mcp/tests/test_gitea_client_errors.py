from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gitea_client import (
    GiteaAuthError,
    GiteaClient,
    GiteaPermissionError,
    GiteaRateLimitError,
    GiteaServerError,
    GiteaTransportError,
    GiteaValidationError,
)


class StubProvider:
    def get_token(self) -> str:
        return "token"


class _ErrorHandler(BaseHTTPRequestHandler):
    status_code = 500

    def do_GET(self) -> None:  # noqa: N802
        payload = b'{"message": "boom"}'
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve(status_code: int):
    handler = type(f"ErrorHandler{status_code}", (_ErrorHandler,), {"status_code": status_code})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, GiteaAuthError),
        (403, GiteaPermissionError),
        (422, GiteaValidationError),
        (429, GiteaRateLimitError),
        (500, GiteaServerError),
    ],
)
def test_http_errors_map_to_specific_exceptions(status_code, error_type):
    server, thread = _serve(status_code)
    try:
        client = GiteaClient(base_url=f"http://127.0.0.1:{server.server_port}/api/v1", provider=StubProvider())
        with pytest.raises(error_type):
            client.whoami()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_transport_errors_are_wrapped():
    client = GiteaClient(base_url="http://127.0.0.1:1/api/v1", provider=StubProvider(), timeout=0.1)
    with pytest.raises(GiteaTransportError):
        client.whoami()
