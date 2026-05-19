from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gitea_client import GiteaAuthError
from gitea_watcher import GiteaWatcher


class StubClient:
    def __init__(self):
        self.comments: list[tuple[str, int, str]] = []

    def list_assigned_issues(self, state="open"):
        return [{"number": 7, "updated_at": "2026-05-19T10:00:00Z", "repository": {"full_name": "K2Lab/demo"}}]

    def get_issue(self, repo, number):
        return {
            "number": number,
            "updated_at": "2026-05-19T10:00:00Z",
            "repository": {"full_name": repo},
            "user": {"login": "alice"},
            "title": "x",
            "state": "open",
            "html_url": "https://example/1",
            "labels": [],
            "body": "如何验证：ok",
        }

    def list_issue_comments(self, repo, number):
        return []

    def comment_on_issue(self, repo, number, body):
        self.comments.append((repo, number, body))
        return {}

    def transition_issue(self, repo, number, state):
        return {"state": state}


def test_record_error_auth_returns_78(tmp_path):
    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient())
    assert watcher.record_error(GiteaAuthError("bad token")) == 78


def test_run_once_marks_pending_retry_when_at_capacity(tmp_path):
    client = StubClient()
    watcher = GiteaWatcher(court_root=tmp_path, client=client)
    watcher.max_concurrent_courts = 0
    watcher._dispatch_shenli = lambda _: {
        "decision": "GO",
        "court_project_name": "issue-k2lab-demo-7",
        "session": "agent-court-issue-k2lab-demo-7",
        "branch_prefix": "auto/issue-7/",
        "agent_team_plan": {"roles": [], "dispatch_message": "work"},
    }
    watcher._ensure_dirs()
    watcher._atomic_write_json(
        watcher.seen_path,
        {
            "K2Lab/demo#7": {
                "repo": "K2Lab/demo",
                "number": 7,
                "updated_at": "2026-05-19T09:00:00Z",
                "last_action": "BOOTSTRAP",
                "court_project": "",
                "shenli_run_at": "2026-05-19T09:00:00Z",
            }
        },
    )
    result = watcher.run_once()
    seen = json.loads((tmp_path / "gitea-watcher" / "seen-issues.json").read_text())
    entry = seen["K2Lab/demo#7"]
    assert result["updated"] == 1
    assert entry["last_action"] == "PENDING_RETRY"
    assert "retry_at" in entry


def test_apply_decision_reuses_existing_project_and_env_whitelist(tmp_path, monkeypatch):
    client = StubClient()
    watcher = GiteaWatcher(court_root=tmp_path, client=client)
    project = "issue-k2lab-demo-7"
    (tmp_path / "projects" / project).mkdir(parents=True)
    os.environ["K2LAB_GIT_TOKEN"] = "secret"
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(cmd, env=None, **kwargs):
        calls.append((cmd, env))
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr("gitea_watcher.subprocess.run", fake_run)
    monkeypatch.setattr("gitea_watcher.server.dispatch_to_foreman", lambda *args, **kwargs: None)
    decision = {
        "decision": "GO",
        "court_project_name": project,
        "session": "agent-court-issue-k2lab-demo-7",
        "branch_prefix": "auto/issue-7/",
        "agent_team_plan": {"roles": [], "dispatch_message": "work"},
    }
    issue = {"number": 7, "repository": {"full_name": "K2Lab/demo"}, "updated_at": "2026-05-19T10:00:00Z"}
    result = watcher._apply_decision(issue, decision)
    assert result["last_action"] == "GO"
    assert len(calls) == 1
    assert calls[0][0][0].endswith("court-up")
    assert "K2LAB_GIT_TOKEN" not in calls[0][1]
    assert calls[0][1]["COURT_UP_NO_ATTACH"] == "1"


def test_atomic_write_round_trip(tmp_path):
    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient())
    watcher._ensure_dirs()
    payload = {"a": 1}
    watcher._atomic_write_json(watcher.seen_path, payload)
    assert json.loads(watcher.seen_path.read_text()) == payload
