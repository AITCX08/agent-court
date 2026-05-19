from __future__ import annotations

import re
import subprocess

SESSION_NAME = "agent-court-dashboard"
WATCHER_WINDOW = "watcher"
_SAFE_WINDOW_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _run_tmux(*args: str, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=True,
        text=True,
        capture_output=capture_output,
    )


def session_exists() -> bool:
    try:
        _run_tmux("has-session", "-t", SESSION_NAME)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def ensure_session() -> None:
    if session_exists():
        return
    _run_tmux("new-session", "-d", "-s", SESSION_NAME, "-n", WATCHER_WINDOW)


def issue_window_name(repo: str, num: int) -> str:
    cleaned = _SAFE_WINDOW_CHARS.sub("-", repo).strip("-")
    if not cleaned:
        raise ValueError("repo must contain at least one tmux-safe character")
    return f"{cleaned}-{int(num)}"


def window_exists(name: str) -> bool:
    return name in list_windows()


def list_windows() -> list[str]:
    proc = _run_tmux("list-windows", "-t", SESSION_NAME, "-F", "#{window_name}", capture_output=True)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def new_issue_window(repo: str, num: int, launch_cmd: str) -> str:
    ensure_session()
    name = issue_window_name(repo, num)
    if not window_exists(name):
        _run_tmux("new-window", "-t", SESSION_NAME, "-n", name)
    inject(name, launch_cmd, send_enter=True)
    return name


def _safe_inject_text(text: str) -> list[str]:
    for ch in text:
        code = ord(ch)
        if code in (9, 10):
            continue
        if code < 32 or code == 127:
            raise ValueError(f"unsupported control character: 0x{code:02x}")
    return text.split("\n")


def inject(name: str, text: str, *, send_enter: bool = True) -> None:
    for index, chunk in enumerate(_safe_inject_text(text)):
        if chunk:
            _run_tmux("send-keys", "-t", f"{SESSION_NAME}:{name}", "-l", chunk)
        if index < len(text.split("\n")) - 1:
            _run_tmux("send-keys", "-t", f"{SESSION_NAME}:{name}", "Enter")
    if send_enter:
        _run_tmux("send-keys", "-t", f"{SESSION_NAME}:{name}", "Enter")


def kill_window(name: str) -> None:
    _run_tmux("kill-window", "-t", f"{SESSION_NAME}:{name}")


def attach() -> None:
    _run_tmux("attach", "-t", SESSION_NAME)
