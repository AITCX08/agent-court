from __future__ import annotations

import subprocess

import pytest

import dashboard_tmux


def test_issue_window_name_sanitizes_repo():
    assert dashboard_tmux.issue_window_name("K2Lab/moras finder", 42) == "K2Lab-moras-finder-42"


def test_safe_inject_rejects_control_characters():
    with pytest.raises(ValueError):
        dashboard_tmux._safe_inject_text("bad\x07bell")


def test_safe_inject_preserves_literals():
    assert dashboard_tmux._safe_inject_text(";rm -rf /\n改 X") == [";rm -rf /", "改 X"]


def test_new_issue_window_uses_literal_send_keys(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, check, text, capture_output):
        calls.append(cmd)
        if cmd[1:3] == ["list-windows", "-t"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="watcher\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(dashboard_tmux.subprocess, "run", fake_run)

    name = dashboard_tmux.new_issue_window("K2Lab/demo", 7, 'echo ";rm -rf /"')

    assert name == "K2Lab-demo-7"
    assert ["tmux", "new-window", "-t", "agent-court-dashboard", "-n", "K2Lab-demo-7"] in calls
    assert ["tmux", "send-keys", "-t", "agent-court-dashboard:K2Lab-demo-7", "-l", 'echo ";rm -rf /"'] in calls
    assert ["tmux", "send-keys", "-t", "agent-court-dashboard:K2Lab-demo-7", "Enter"] in calls


def test_inject_sends_multiline_as_literal_chunks(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, check, text, capture_output):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(dashboard_tmux.subprocess, "run", fake_run)

    dashboard_tmux.inject("demo", "改 X\n确认", send_enter=True)

    assert calls == [
        ["tmux", "send-keys", "-t", "agent-court-dashboard:demo", "-l", "改 X"],
        ["tmux", "send-keys", "-t", "agent-court-dashboard:demo", "Enter"],
        ["tmux", "send-keys", "-t", "agent-court-dashboard:demo", "-l", "确认"],
        ["tmux", "send-keys", "-t", "agent-court-dashboard:demo", "Enter"],
    ]
