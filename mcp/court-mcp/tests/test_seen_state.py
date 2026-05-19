"""seen_state.py helper 并发安全测试 (PR-13 review C4 fix)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import seen_state


def test_atomic_write_then_load_roundtrip(tmp_path):
    state_dir = tmp_path / "gitea-watcher"
    seen_state.atomic_write_seen_issues({"K2Lab/x#1": {"last_action": "GO"}}, state_dir)
    data = seen_state.load_seen(state_dir)
    assert data == {"K2Lab/x#1": {"last_action": "GO"}}


def test_update_entry_merges_patch(tmp_path):
    state_dir = tmp_path / "gitea-watcher"
    # seed
    seen_state.atomic_write_seen_issues({"K2Lab/x#1": {"last_action": "AWAITING_INTAKE_APPROVAL", "repo": "K2Lab/x"}}, state_dir)
    seen_state.update_entry("K2Lab/x", 1, {"last_action": "DISPATCHED_DASHBOARD", "approval_winner": "terminal"}, state_dir)
    data = seen_state.load_seen(state_dir)
    entry = data["K2Lab/x#1"]
    assert entry["last_action"] == "DISPATCHED_DASHBOARD"
    assert entry["approval_winner"] == "terminal"
    assert entry["repo"] == "K2Lab/x"  # 原有字段保留


def test_concurrent_updates_no_lost_writes(tmp_path):
    """多线程并发 update_entry, 最终所有 patch 都该体现 (state_lock 互斥)."""
    state_dir = tmp_path / "gitea-watcher"
    seen_state.atomic_write_seen_issues({}, state_dir)
    # 10 个线程, 各 update 不同 key, 期望 10 个 key 全部出现
    threads: list[threading.Thread] = []
    for i in range(10):
        repo = f"K2Lab/x{i}"
        t = threading.Thread(target=seen_state.update_entry, args=(repo, i, {"last_action": "AWAITING_INTAKE_APPROVAL", "tag": f"t{i}"}, state_dir))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    data = seen_state.load_seen(state_dir)
    assert len(data) == 10
    for i in range(10):
        key = f"K2Lab/x{i}#{i}"
        assert key in data, f"missing {key}"
        assert data[key]["tag"] == f"t{i}"


def test_concurrent_same_key_serial_merge(tmp_path):
    """同一 key 多线程 update, 不丢字段 (因为 update_entry 内部 lock 包住 load->merge->write)."""
    state_dir = tmp_path / "gitea-watcher"
    seen_state.atomic_write_seen_issues({"K2Lab/y#7": {"last_action": "INIT"}}, state_dir)
    threads: list[threading.Thread] = []
    for i in range(10):
        t = threading.Thread(target=seen_state.update_entry, args=("K2Lab/y", 7, {f"field_{i}": i}, state_dir))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    data = seen_state.load_seen(state_dir)
    entry = data["K2Lab/y#7"]
    # 所有 field_0..9 都该存在
    for i in range(10):
        assert entry[f"field_{i}"] == i
    # 原有字段也保留
    assert entry["last_action"] == "INIT"


def test_state_lock_blocks_until_release(tmp_path):
    """另一进程 (这里用线程模拟) 在 lock 内, 当前线程必须等."""
    state_dir = tmp_path / "gitea-watcher"
    seen_state.atomic_write_seen_issues({}, state_dir)
    released = threading.Event()
    other_done = threading.Event()

    def holder():
        with seen_state.state_lock(state_dir):
            released.wait(timeout=2.0)

    h = threading.Thread(target=holder)
    h.start()
    # 让 holder 先抢到锁
    import time
    time.sleep(0.1)

    def follower():
        with seen_state.state_lock(state_dir):
            other_done.set()

    f = threading.Thread(target=follower)
    f.start()
    # follower 应当被 block
    assert not other_done.wait(timeout=0.3), "follower 不该在 holder 释放前拿到锁"
    released.set()
    h.join(timeout=2.0)
    assert other_done.wait(timeout=2.0), "holder 释放后 follower 应当能拿到锁"
    f.join(timeout=2.0)
