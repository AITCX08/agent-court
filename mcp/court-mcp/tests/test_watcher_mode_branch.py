"""watcher dashboard 模式分支测试 (PR-13 review C5 之后).

C5 修法把 _apply_decision_dashboard 改成异步消息驱动:
- 不再同步调 request_intake 等审批
- 改成 queue_intake (写 pending + 推 IM + 立即 return)
- last_action 改为 AWAITING_INTAKE_APPROVAL
- spawn 由 ImReplyRouter 收到 approve .result 后做
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gitea_watcher import GiteaWatcher


class StubClient:
    def __init__(self) -> None:
        self.commented: list[tuple[str, int, str]] = []
        self.transitioned: list[tuple[str, int, str]] = []

    def list_issue_comments(self, repo: str, num: int) -> list[dict[str, Any]]:
        return []

    def comment_on_issue(self, repo: str, num: int, body: str) -> dict:
        self.commented.append((repo, num, body))
        return {}

    def transition_issue(self, repo: str, num: int, state: str) -> dict:
        self.transitioned.append((repo, num, state))
        return {}


def test_dashboard_go_queues_intake_async_and_writes_context(monkeypatch, tmp_path):
    """dashboard 模式 GO 现在异步: queue pending + 推 IM, 不等审批, 不直接 spawn."""
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    queued: list[dict] = []

    class StubStore:
        def __init__(self, *args, **kwargs):
            pass

        def queue_intake(self, repo, num, issue, decision):
            queued.append({"repo": repo, "num": num})
            return {"slug_id": f"{repo.replace('/', '-').lower()}-{num}-intake", "msg_id": "abc123"}

    monkeypatch.setattr("gitea_watcher.ApprovalStore", StubStore)

    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient(), mode="dashboard")
    issue = {
        "number": 7,
        "repository": {"full_name": "K2Lab/demo"},
        "title": "fixture",
        "html_url": "http://localhost/K2Lab/demo/issues/7",
        "body": "fixture body",
    }
    decision = {"decision": "GO", "court_project_name": "issue-k2lab-demo-7", "branch_prefix": "auto/issue-7/", "agent_team_plan": {}}
    result = watcher._apply_decision_dashboard(issue, decision)
    # 不再同步走 spawn
    assert result["last_action"] == "AWAITING_INTAKE_APPROVAL"
    assert result["stage"] == "INTAKE"
    assert result["intake_slug_id"] == "k2lab-demo-7-intake"
    assert len(queued) == 1
    # 写了 intake-context
    ctx = tmp_path / "gitea-watcher" / "pending-intake-context" / "k2lab-demo-7.json"
    assert ctx.exists()
    payload = json.loads(ctx.read_text())
    assert payload["issue"]["number"] == 7
    assert payload["decision"]["decision"] == "GO"


def test_dashboard_diff_skips_inflight_actions(tmp_path):
    """dashboard 模式下 issue 已经在跑 (DISPATCHED_DASHBOARD/EXECUTING/AWAITING_*),
    即使 updated_at 变了也不重新触发."""
    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient(), mode="dashboard")
    current = [
        {"number": 1, "updated_at": "2026-05-19T20:00:00Z", "repository": {"full_name": "K2Lab/a"}},  # 新的
        {"number": 2, "updated_at": "2026-05-19T20:05:00Z", "repository": {"full_name": "K2Lab/b"}},  # updated, dispatched
        {"number": 3, "updated_at": "2026-05-19T20:06:00Z", "repository": {"full_name": "K2Lab/c"}},  # updated, awaiting
        {"number": 4, "updated_at": "2026-05-19T20:07:00Z", "repository": {"full_name": "K2Lab/d"}},  # updated, executing
        {"number": 5, "updated_at": "2026-05-19T20:08:00Z", "repository": {"full_name": "K2Lab/e"}},  # updated, GO done (court mode), 重 trigger
    ]
    seen = {
        "K2Lab/b#2": {"repo": "K2Lab/b", "number": 2, "updated_at": "2026-05-19T19:00:00Z", "last_action": "DISPATCHED_DASHBOARD"},
        "K2Lab/c#3": {"repo": "K2Lab/c", "number": 3, "updated_at": "2026-05-19T19:00:00Z", "last_action": "AWAITING_INTAKE_APPROVAL"},
        "K2Lab/d#4": {"repo": "K2Lab/d", "number": 4, "updated_at": "2026-05-19T19:00:00Z", "last_action": "EXECUTING"},
        "K2Lab/e#5": {"repo": "K2Lab/e", "number": 5, "updated_at": "2026-05-19T19:00:00Z", "last_action": "GO"},
    }
    new_items, updated_items = watcher._diff(current, seen)
    new_keys = sorted([f"{watcher._issue_repo(x)}#{x['number']}" for x in new_items])
    upd_keys = sorted([f"{watcher._issue_repo(x)}#{x['number']}" for x in updated_items])
    assert new_keys == ["K2Lab/a#1"]
    # dashboard inflight 全部不触发, 只有 K2Lab/e#5 (GO 不在 inflight 集合里) 会进 updated
    assert upd_keys == ["K2Lab/e#5"]


def test_court_mode_diff_still_triggers_on_update(tmp_path):
    """court 模式下 (PR-12 默认) dashboard 守卫不生效, updated_at 变就重新触发."""
    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient(), mode="court")
    current = [{"number": 1, "updated_at": "2026-05-19T20:00:00Z", "repository": {"full_name": "K2Lab/x"}}]
    seen = {"K2Lab/x#1": {"repo": "K2Lab/x", "number": 1, "updated_at": "2026-05-19T19:00:00Z", "last_action": "DISPATCHED_DASHBOARD"}}
    new_items, updated_items = watcher._diff(current, seen)
    # court 模式不区分 dashboard 状态, updated_at 变就视为 updated
    assert len(updated_items) == 1
