from __future__ import annotations

import json

from issue_resolver import build_intro_message, report_back


def test_build_intro_message_contains_issue_decision_and_comments():
    issue = {"number": 7, "repository": {"full_name": "K2Lab/demo"}}
    comments = [{"body": "a"}]
    decision = {"decision": "GO"}
    text = build_intro_message(issue, comments, decision)
    assert "ISSUE_RESOLVER_BEGIN K2Lab/demo 7" in text
    assert '"decision": "GO"' in text
    assert '"body": "a"' in text


def test_report_back_marks_done(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    state_dir = tmp_path / "gitea-watcher"
    state_dir.mkdir(parents=True)
    seen = state_dir / "seen-issues.json"
    seen.write_text(json.dumps({"K2Lab/demo#7": {"last_action": "EXECUTING", "stage": "EXECUTING"}}, ensure_ascii=False))
    result = report_back("K2Lab/demo", 7, "done")
    payload = json.loads(seen.read_text())
    assert result["ok"] is True
    assert payload["K2Lab/demo#7"]["last_action"] == "DONE_DASHBOARD"
