"""ImReplyRouter: dashboard 模式下的异步审批结果路由器.

监听 ``pending-approval/*-intake.result``:
- approve → 加载对应 ``pending-intake-context/<slug>-<num>.json`` (watcher 落的 issue+decision 上下文),
  调 ``bin/spawn-issue-window`` 起 Claude window, 更新 seen-issues.json last_action=DISPATCHED_DASHBOARD
- reject → 评论 + close issue, 更新 seen-issues.json last_action=REJECTED_DASHBOARD

PR-13 不再监听 PLAN result (C6): plan 阶段由 ``dual_channel_approval.request_plan`` 内部
``_wait_for_result`` 自己 drain, Claude window 内部阻塞读 verdict, 不需要 router 注入.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import seen_state


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ImReplyRouter:
    def __init__(
        self,
        court_root: Path,
        *,
        poll_interval: float = 1.0,
        gitea_client=None,
        spawn_window_bin: Path | None = None,
    ) -> None:
        self.court_root = court_root
        self.poll_interval = poll_interval
        self.pending_dir = self.court_root / "gitea-watcher" / "pending-approval"
        self.ctx_dir = self.court_root / "gitea-watcher" / "pending-intake-context"
        self.processed_dir = self.pending_dir / ".processed"
        self._seen_results: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gitea_client = gitea_client
        # 默认 bin 路径: <repo_root>/bin/spawn-issue-window
        self.spawn_window_bin = spawn_window_bin or (Path(__file__).resolve().parents[2] / "bin" / "spawn-issue-window")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="im-reply-router", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:  # pragma: no cover - defensive: 线程不能死
                print(f"[router] scan failed: {exc!r}", file=sys.stderr, flush=True)
            time.sleep(self.poll_interval)

    def scan_once(self) -> int:
        """单步扫描, 返回处理的 result 数. 测试用."""
        return self._scan_once()

    def _scan_once(self) -> int:
        if not self.pending_dir.is_dir():
            return 0
        count = 0
        for result_path in sorted(self.pending_dir.glob("*-intake.result")):
            if result_path.name in self._seen_results:
                continue
            try:
                self._handle_intake_result(result_path)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[router] handle {result_path.name} failed: {exc!r}", file=sys.stderr, flush=True)
            self._seen_results.add(result_path.name)
            count += 1
        return count

    def _handle_intake_result(self, result_path: Path) -> None:
        try:
            meta = json.loads(result_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"[router] {result_path.name} 不是合法 JSON: {exc}; 已跳过", file=sys.stderr, flush=True)
            self._archive(result_path, reason="invalid-json")
            return

        repo = meta.get("repo", "")
        num = int(meta.get("number", 0))
        verdict = meta.get("verdict", "")
        winner = meta.get("winner", "?")

        ctx = self._load_intake_context(repo, num)
        if ctx is None:
            print(f"[router] missing intake context for {repo}#{num}; 已跳过", file=sys.stderr, flush=True)
            self._archive(result_path, reason="missing-context")
            return

        if verdict == "approve":
            self._dispatch_approved(repo, num, ctx, winner)
        elif verdict == "reject":
            self._dispatch_rejected(repo, num, meta.get("reason", ""), winner)
        else:
            print(f"[router] unsupported verdict {verdict!r} for {repo}#{num}", file=sys.stderr, flush=True)

        self._archive(result_path, reason=verdict or "unknown")

    def _dispatch_approved(self, repo: str, num: int, ctx: dict[str, Any], winner: str) -> None:
        issue = ctx["issue"]
        decision = ctx["decision"]
        comments = ctx.get("comments", [])

        # 写 intro 给 spawn-issue-window 加载
        from issue_resolver import build_intro_message
        intro = build_intro_message(issue, comments, decision)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
            handle.write(intro)
            intro_path = handle.name

        try:
            subprocess.run(
                [str(self.spawn_window_bin), repo, str(num), intro_path],
                check=True,
                env=self._safe_env(),
            )
        except subprocess.CalledProcessError as exc:
            print(f"[router] spawn-issue-window failed for {repo}#{num}: {exc}", file=sys.stderr, flush=True)
            seen_state.update_entry(repo, num, {
                "last_action": "SPAWN_FAILED",
                "approval_winner": winner,
                "stage": "INTAKE",
                "spawn_error": str(exc),
            })
            return

        from dashboard_tmux import issue_window_name
        window_name = issue_window_name(repo, num)
        seen_state.update_entry(repo, num, {
            "last_action": "DISPATCHED_DASHBOARD",
            "approval_winner": winner,
            "tmux_window": window_name,
            "dispatched_at": _iso_now(),
            "stage": "INTAKE",
        })

    def _dispatch_rejected(self, repo: str, num: int, reason: str, winner: str) -> None:
        client = self._client()
        try:
            comment_body = reason.strip() or f"intake 审批未通过 (by {winner})"
            client.comment_on_issue(repo, num, comment_body)
            client.transition_issue(repo, num, "closed")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[router] reject api call failed for {repo}#{num}: {exc!r}", file=sys.stderr, flush=True)
        seen_state.update_entry(repo, num, {
            "last_action": "REJECTED_DASHBOARD",
            "approval_winner": winner,
            "stage": "INTAKE",
        })

    def _load_intake_context(self, repo: str, num: int) -> dict[str, Any] | None:
        slug = repo.replace("/", "-").lower()
        ctx_path = self.ctx_dir / f"{slug}-{num}.json"
        if not ctx_path.is_file():
            return None
        try:
            return json.loads(ctx_path.read_text())
        except json.JSONDecodeError:
            return None

    def _archive(self, result_path: Path, *, reason: str) -> None:
        """把处理过的 .result 移到 .processed/, 避免下一轮重复 scan."""
        try:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            target = self.processed_dir / f"{result_path.stem}.{reason}.json"
            result_path.rename(target)
        except OSError:
            try:
                result_path.unlink()
            except OSError:
                pass

    def _client(self):
        if self._gitea_client is None:
            from gitea_client import GiteaClient
            self._gitea_client = GiteaClient()
        return self._gitea_client

    @staticmethod
    def _safe_env() -> dict[str, str]:
        keys = {"PATH", "HOME", "USER", "SHELL", "TERM", "TMPDIR", "COURT_ROOT", "LANG", "LC_ALL"}
        return {k: v for k, v in os.environ.items() if k in keys}
