from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server


class StubClient:
    def list_assigned_issues(self, state="open", since=None):
        return [{"number": 1, "state": state, "since": since}]

    def get_issue(self, repo, number):
        return {"repo": repo, "number": number}

    def comment_on_issue(self, repo, number, body):
        return {"repo": repo, "number": number, "body": body}

    def transition_issue(self, repo, number, state):
        return {"repo": repo, "number": number, "state": state}


def test_list_assigned_issues_tool(monkeypatch):
    monkeypatch.setattr(server, "GiteaClient", lambda: StubClient())
    out = server.list_assigned_issues(state="all", since="2026-05-01T00:00:00Z")
    assert out["count"] == 1
    assert out["issues"][0]["state"] == "all"


def test_get_issue_tool(monkeypatch):
    monkeypatch.setattr(server, "GiteaClient", lambda: StubClient())
    out = server.get_issue("K2Lab/demo", 3)
    assert out["number"] == 3


def test_comment_on_issue_tool(monkeypatch):
    monkeypatch.setattr(server, "GiteaClient", lambda: StubClient())
    out = server.comment_on_issue("K2Lab/demo", 3, "hello")
    assert out["body"] == "hello"


def test_transition_issue_tool(monkeypatch):
    monkeypatch.setattr(server, "GiteaClient", lambda: StubClient())
    ok = server.transition_issue("K2Lab/demo", 3, "closed")
    bad = server.transition_issue("K2Lab/demo", 3, "x")
    assert ok["state"] == "closed"
    assert bad["error"] == "invalid_state"
