"""dual_channel_approval race / 仲裁测试.

PR-13 review W4 之后从单 case 扩到 4 case:
1. 同毫秒级 race 时只有 1 个 winner (terminal/IM)
2. terminal 失败后 IM 仍能接管
3. 双 IM (feishu + wechat) 同时回复 winner 唯一
4. 崩溃后 (lock 文件残留但 .result 未写) 重新抢锁能拿到
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from dual_channel_approval import ApprovalStore


def _make_store(tmp_path: Path) -> ApprovalStore:
    store = ApprovalStore(court_root=tmp_path)
    store.pending_dir.mkdir(parents=True, exist_ok=True)
    return store


def test_intake_race_single_winner(tmp_path):
    """同毫秒级 3 通道竞争, 应该只有 1 个 winner."""
    store = _make_store(tmp_path)
    slug_id, _ = store._slug_id("K2Lab/demo", 42, "INTAKE")
    store._paths(slug_id)["json"].write_text("{}")
    barrier = threading.Barrier(4)
    results: list[bool] = []
    winners: list[str] = []

    def act(winner: str, grace: float) -> None:
        barrier.wait()
        ok = store.submit_verdict("K2Lab/demo", 42, stage="INTAKE", verdict="approve", winner=winner, grace_seconds=grace)
        results.append(ok)
        if ok:
            winners.append(winner)

    threads = [
        threading.Thread(target=act, args=("terminal", 0.0)),
        threading.Thread(target=act, args=("feishu", 0.0)),
        threading.Thread(target=act, args=("wechat", 0.0)),
    ]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()
    assert results.count(True) == 1, f"expected single winner, results={results}"
    assert len(winners) == 1
    # .result 文件确实落盘了
    result_path = store._paths(slug_id)["result"]
    payload = json.loads(result_path.read_text())
    assert payload["winner"] == winners[0]


def test_im_takes_over_when_terminal_does_not_respond(tmp_path):
    """terminal 长时间没动作 (没调 submit_verdict), IM 通道仍能成功 commit verdict."""
    store = _make_store(tmp_path)
    slug_id, _ = store._slug_id("K2Lab/demo", 43, "INTAKE")
    store._paths(slug_id)["json"].write_text("{}")
    # 模拟只有 IM (feishu) 提交, terminal 沉默
    ok = store.submit_verdict("K2Lab/demo", 43, stage="INTAKE", verdict="approve", winner="feishu")
    assert ok
    payload = json.loads(store._paths(slug_id)["result"].read_text())
    assert payload["winner"] == "feishu"


def test_double_im_channels_race_single_winner(tmp_path):
    """feishu + wechat 同时 reply, 只能 1 个 winner."""
    store = _make_store(tmp_path)
    slug_id, _ = store._slug_id("K2Lab/demo", 44, "INTAKE")
    store._paths(slug_id)["json"].write_text("{}")
    barrier = threading.Barrier(3)
    results: list[tuple[str, bool]] = []

    def act(winner: str) -> None:
        barrier.wait()
        ok = store.submit_verdict("K2Lab/demo", 44, stage="INTAKE", verdict="approve", winner=winner)
        results.append((winner, ok))

    a = threading.Thread(target=act, args=("feishu",))
    b = threading.Thread(target=act, args=("wechat",))
    a.start()
    b.start()
    barrier.wait()
    a.join()
    b.join()
    won = [w for w, ok in results if ok]
    assert len(won) == 1, f"both channels won: {results}"
    payload = json.loads(store._paths(slug_id)["result"].read_text())
    assert payload["winner"] == won[0]


def test_lock_residue_does_not_block_next_attempt(tmp_path):
    """先前进程崩溃留下 lock 文件, 下一轮抢锁仍能拿到 (fcntl.flock 进程退出会自动释放,
    但磁盘上的空 lock 文件文件本身存在). 验证重新 open + flock 仍 OK."""
    store = _make_store(tmp_path)
    slug_id, _ = store._slug_id("K2Lab/demo", 45, "INTAKE")
    paths = store._paths(slug_id)
    paths["json"].write_text("{}")
    # 模拟先前残留的 lock 文件
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    paths["lock"].write_text("")
    # 新一轮提交仍能成功
    ok = store.submit_verdict("K2Lab/demo", 45, stage="INTAKE", verdict="approve", winner="terminal")
    assert ok
    payload = json.loads(paths["result"].read_text())
    assert payload["winner"] == "terminal"


def test_second_submit_after_resolved_returns_false(tmp_path):
    """已经有 winner 后, 第二个通道再 reply 应该收到 False (避免双重处理)."""
    store = _make_store(tmp_path)
    slug_id, _ = store._slug_id("K2Lab/demo", 46, "INTAKE")
    store._paths(slug_id)["json"].write_text("{}")
    first = store.submit_verdict("K2Lab/demo", 46, stage="INTAKE", verdict="approve", winner="terminal")
    assert first
    # 第二个通道随后再来
    second = store.submit_verdict("K2Lab/demo", 46, stage="INTAKE", verdict="reject", winner="feishu", grace_seconds=0.05)
    assert not second
    # 最终 .result 仍是第一个 winner 的
    payload = json.loads(store._paths(slug_id)["result"].read_text())
    assert payload["winner"] == "terminal"
    assert payload["verdict"] == "approve"
