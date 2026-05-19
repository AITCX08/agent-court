from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gitea_client import GiteaClient, GiteaNotFoundError


class StubProvider:
    def get_token(self) -> str:
        return "token"


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/v1/user":
            self._send(200, {"login": "alice"})
            return
        if parsed.path == "/api/v1/repos/issues/search":
            page = int(query.get("page", ["1"])[0])
            if page == 1:
                self._send(
                    200,
                    [
                        {"number": 1, "repository": {"full_name": "K2Lab/demo"}, "updated_at": "2026-05-19T10:00:00Z"},
                        {"number": 2, "repository": {"full_name": "K2Lab/demo"}, "updated_at": "2026-05-19T10:01:00Z", "pull_request": {"url": "x"}},
                    ],
                )
            else:
                self._send(200, [])
            return
        if parsed.path == "/api/v1/repos/K2Lab/demo/issues/404":
            self._send(404, {"message": "missing"})
            return
        if parsed.path == "/api/v1/repos/K2Lab/demo/issues/1":
            self._send(200, {"number": 1, "repository": {"full_name": "K2Lab/demo"}})
            return
        if parsed.path == "/api/v1/repos/K2Lab/demo/issues/1/comments":
            page = int(query.get("page", ["1"])[0])
            if page == 1:
                self._send(200, [{"id": 1, "body": "ok"}])
            else:
                self._send(200, [])
            return
        self._send(404, {"message": "unknown"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/v1/repos/K2Lab/demo/issues/1/comments":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self._send(201, {"body": payload["body"]})
            return
        self._send(404, {"message": "unknown"})

    def do_PATCH(self) -> None:  # noqa: N802
        if self.path == "/api/v1/repos/K2Lab/demo/issues/1":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self._send(200, {"state": payload["state"]})
            return
        self._send(404, {"message": "unknown"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


@pytest.fixture
def api_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_whoami(api_server):
    client = GiteaClient(base_url=f"http://127.0.0.1:{api_server.server_port}/api/v1", provider=StubProvider(), per_page=2)
    assert client.whoami()["login"] == "alice"


def test_list_assigned_issues_filters_pull_requests(api_server):
    client = GiteaClient(base_url=f"http://127.0.0.1:{api_server.server_port}/api/v1", provider=StubProvider(), per_page=2)
    issues = client.list_assigned_issues()
    assert len(issues) == 1
    assert issues[0]["number"] == 1


def test_get_issue(api_server):
    client = GiteaClient(base_url=f"http://127.0.0.1:{api_server.server_port}/api/v1", provider=StubProvider(), per_page=2)
    issue = client.get_issue("K2Lab/demo", 1)
    assert issue["number"] == 1


def test_get_issue_404(api_server):
    client = GiteaClient(base_url=f"http://127.0.0.1:{api_server.server_port}/api/v1", provider=StubProvider(), per_page=2)
    with pytest.raises(GiteaNotFoundError):
        client.get_issue("K2Lab/demo", 404)


def test_comment_transition_and_comment_pagination(api_server):
    client = GiteaClient(base_url=f"http://127.0.0.1:{api_server.server_port}/api/v1", provider=StubProvider(), per_page=2)
    assert client.comment_on_issue("K2Lab/demo", 1, "ping")["body"] == "ping"
    assert client.transition_issue("K2Lab/demo", 1, "closed")["state"] == "closed"
    comments = client.list_issue_comments("K2Lab/demo", 1)
    assert comments == [{"id": 1, "body": "ok"}]
