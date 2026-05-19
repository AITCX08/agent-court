from __future__ import annotations

import json
import threading

from dual_channel_approval import ApprovalStore, _parse_terminal_reply


def test_submit_verdict_allows_only_one_winner(tmp_path):
    store = ApprovalStore(court_root=tmp_path)
    issue = {"title": "Fix ETL", "html_url": "https://example/1"}
    decision = {"decision": "GO", "court_project_name": "issue-1"}
    slug_id, _ = store._slug_id("K2Lab/demo", 7, "INTAKE")
    paths = store._paths(slug_id)
    store.pending_dir.mkdir(parents=True, exist_ok=True)
    paths["json"].write_text("{}")

    barrier = threading.Barrier(3)
    results: list[bool] = []

    def worker(winner: str) -> None:
        barrier.wait()
        results.append(store.submit_verdict("K2Lab/demo", 7, stage="INTAKE", verdict="approve", winner=winner))

    threads = [
        threading.Thread(target=worker, args=("terminal",)),
        threading.Thread(target=worker, args=("feishu",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    payload = json.loads(paths["result"].read_text())
    assert payload["winner"] in {"terminal", "feishu"}


def test_parse_terminal_reply_supports_approve_edit_reject():
    assert _parse_terminal_reply("可以") == ("approve", "", "")
    assert _parse_terminal_reply("改 调整日志") == ("edit", "", "调整日志")
    assert _parse_terminal_reply("拒 风险太高") == ("reject", "风险太高", "")


def test_wait_for_result_reads_im_bridge(tmp_path):
    store = ApprovalStore(court_root=tmp_path)
    meta = {
        "repo": "K2Lab/demo",
        "number": 7,
        "stage": "INTAKE",
        "slug_id": "k2lab-demo-7-intake",
        "msg_id": "deadbeefcafe",
    }
    store._ensure_shenpi_project()
    inbox = store.project_dir / "bus" / "dashboard" / "inbox"
    msg = inbox / "1-deadbeefcafe-dashboard-to-approver.md"
    msg.write_text("---\nid: deadbeefcafe\nfrom: dashboard\nto: approver\n---\n\nok\n")
    audit = store.project_dir / "logs" / "approval-log.jsonl"
    audit.write_text(json.dumps({"msg_id": "deadbeefcafe", "by": "wechat-user"}) + "\n")
    result = store._paths(meta["slug_id"])["result"]
    store.pending_dir.mkdir(parents=True, exist_ok=True)

    store._drain_shenpi_bus(meta)

    payload = json.loads(result.read_text())
    assert payload["verdict"] == "approve"
    assert payload["winner"] == "wechat"
