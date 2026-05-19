"""seen-issues.json 共享状态访问 helper.

watcher 主进程 + issue_resolver 子进程会同时写这个文件;
所有写必须走 ``state_lock()`` + ``atomic_write_seen_issues()``,
避免后写覆盖前写.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def default_state_dir() -> Path:
    return Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court"))) / "gitea-watcher"


def seen_path(state_dir: Path | None = None) -> Path:
    return (state_dir or default_state_dir()) / "seen-issues.json"


def lock_path(state_dir: Path | None = None) -> Path:
    return (state_dir or default_state_dir()) / ".state.lock"


@contextmanager
def state_lock(state_dir: Path | None = None) -> Iterator[None]:
    """文件锁; watcher 主循环 + report_back 共用."""
    lp = lock_path(state_dir)
    lp.parent.mkdir(parents=True, exist_ok=True)
    handle = lp.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_seen(state_dir: Path | None = None) -> dict[str, Any]:
    sp = seen_path(state_dir)
    if not sp.exists():
        return {}
    return json.loads(sp.read_text())


def atomic_write_seen_issues(data: dict[str, Any], state_dir: Path | None = None) -> None:
    """tempfile + os.replace,保证 reader 不会看到半写状态."""
    sp = seen_path(state_dir)
    sp.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=sp.parent,
        prefix=f".{sp.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, sp)


def update_entry(repo: str, num: int, patch: dict[str, Any], state_dir: Path | None = None) -> dict[str, Any]:
    """以 ``state_lock`` 包住 load -> merge -> atomic write 整段, 返回最新 entry."""
    key = f"{repo}#{num}"
    with state_lock(state_dir):
        data = load_seen(state_dir)
        entry = dict(data.get(key) or {})
        entry.update(patch)
        data[key] = entry
        atomic_write_seen_issues(data, state_dir)
        return entry
