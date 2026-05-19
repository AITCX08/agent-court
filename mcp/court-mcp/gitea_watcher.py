from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import server
from dual_channel_approval import ApprovalStore, request_intake
from gitea_client import (
    GiteaAuthError,
    GiteaClient,
    GiteaClientError,
    GiteaNotFoundError,
    GiteaPermissionError,
    GiteaRateLimitError,
    GiteaServerError,
    GiteaTransportError,
)

SAFE_ENV_KEYS = {"PATH", "HOME", "USER", "SHELL", "TERM", "TMPDIR", "COURT_ROOT", "LANG", "LC_ALL"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GiteaWatcher:
    def __init__(
        self,
        poll_interval: int = 30,
        court_root: Path | None = None,
        client: GiteaClient | None = None,
        mode: str = "court",
    ) -> None:
        if mode not in {"court", "dashboard"}:
            raise ValueError(f"unsupported mode: {mode!r}")
        self.poll_interval = poll_interval
        self.court_root = court_root or Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court")))
        self.state_dir = self.court_root / "gitea-watcher"
        self.pending_dir = self.state_dir / "pending-shenli"
        self.seen_path = self.state_dir / "seen-issues.json"
        self.error_path = self.state_dir / "error-state.json"
        self.client = client or GiteaClient()
        self.max_concurrent_courts = int(os.environ.get("MAX_CONCURRENT_COURTS", "5"))
        self.bin_dir = Path(__file__).resolve().parents[2] / "bin"
        self._lock_path = self.state_dir / ".state.lock"
        self.mode = mode

    def loop(self) -> None:
        router = None
        if self.mode == "dashboard":
            from im_reply_router import ImReplyRouter

            router = ImReplyRouter(self.court_root)
            router.start()
        try:
            while True:
                self.run_once()
                time.sleep(self.poll_interval)
        finally:
            if router is not None:
                router.stop()

    def run_once(self) -> dict[str, int]:
        self._ensure_dirs()
        with self._state_lock():
            seen = self._load_json(self.seen_path, {})
            issues = self.client.list_assigned_issues(state="open")
            queued = self._merge_retry_candidates(issues, seen)
            if not seen:
                bootstrap = {
                    self._issue_key(item): {
                        "repo": self._issue_repo(item),
                        "number": item["number"],
                        "updated_at": item.get("updated_at", ""),
                        "last_action": "BOOTSTRAP",
                        "court_project": "",
                        "shenli_run_at": _iso_now(),
                    }
                    for item in queued
                }
                self._atomic_write_json(self.seen_path, bootstrap)
                return {"new": 0, "updated": 0, "errors": 0}

            new_items, updated_items = self._diff(queued, seen)
            for item in [*new_items, *updated_items]:
                key = self._issue_key(item)
                try:
                    detail = self.client.get_issue(self._issue_repo(item), int(item["number"]))
                except GiteaPermissionError:
                    seen[key] = self._build_seen_entry(item, last_action="SKIPPED_403")
                    continue
                except GiteaNotFoundError:
                    seen[key] = self._build_seen_entry(item, last_action="SKIPPED_404")
                    continue
                comments = self.client.list_issue_comments(self._issue_repo(item), int(item["number"]))
                pending_file = self._write_pending_file(detail, comments)
                decision = self._dispatch_shenli(pending_file)
                result = self._apply_decision_dashboard(detail, decision) if self.mode == "dashboard" else self._apply_decision(detail, decision)
                entry = self._build_seen_entry(
                    detail,
                    last_action=result["last_action"],
                    court_project=decision.get("court_project_name", ""),
                    retry_at=result.get("retry_at"),
                    approval_winner=result.get("approval_winner"),
                    tmux_window=result.get("tmux_window"),
                    dispatched_at=result.get("dispatched_at"),
                    stage=result.get("stage"),
                )
                # 让 dashboard 模式特有的字段也带进 seen-issues.json (router 用)
                for extra_key in ("intake_slug_id", "intake_msg_id"):
                    if result.get(extra_key):
                        entry[extra_key] = result[extra_key]
                seen[key] = entry

            self._drain_upstream_inboxes(seen)
            self._atomic_write_json(self.seen_path, seen)
            self._reset_errors()
            return {"new": len(new_items), "updated": len(updated_items), "errors": 0}

    def _build_seen_entry(
        self,
        issue: dict[str, Any],
        *,
        last_action: str,
        court_project: str = "",
        retry_at: str | None = None,
        approval_winner: str | None = None,
        tmux_window: str | None = None,
        dispatched_at: str | None = None,
        stage: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "repo": self._issue_repo(issue),
            "number": int(issue["number"]),
            "updated_at": issue.get("updated_at", ""),
            "last_action": last_action,
            "court_project": court_project,
            "shenli_run_at": _iso_now(),
        }
        if retry_at:
            entry["retry_at"] = retry_at
        if approval_winner:
            entry["approval_winner"] = approval_winner
        if tmux_window:
            entry["tmux_window"] = tmux_window
        if dispatched_at:
            entry["dispatched_at"] = dispatched_at
        if stage:
            entry["stage"] = stage
        return entry

    def _merge_retry_candidates(self, current: list[dict[str, Any]], seen: dict[str, Any]) -> list[dict[str, Any]]:
        merged = {self._issue_key(item): item for item in current}
        now = _iso_now()
        for key, entry in seen.items():
            if entry.get("last_action") != "PENDING_RETRY":
                continue
            if entry.get("retry_at", now) > now:
                continue
            if key not in merged:
                merged[key] = {
                    "number": entry["number"],
                    "updated_at": entry.get("updated_at", ""),
                    "repository": {"full_name": entry["repo"]},
                }
        return list(merged.values())

    # 一旦进入这些状态, dashboard 路径里不再 re-spawn / 不再走完整 shenli, 仅写控制塔日志通知
    _DASHBOARD_INFLIGHT_ACTIONS = {
        "AWAITING_INTAKE_APPROVAL",
        "DISPATCHED_DASHBOARD",
        "EXECUTING",
        "AWAITING_PLAN_APPROVAL",
    }

    def _diff(self, current: list[dict[str, Any]], seen: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        new_items: list[dict[str, Any]] = []
        updated_items: list[dict[str, Any]] = []
        for item in current:
            key = self._issue_key(item)
            updated_at = item.get("updated_at", "")
            seen_entry = seen.get(key)
            if seen_entry is None:
                new_items.append(item)
            elif seen_entry.get("last_action") == "PENDING_RETRY":
                updated_items.append(item)
            elif self.mode == "dashboard" and seen_entry.get("last_action") in self._DASHBOARD_INFLIGHT_ACTIONS:
                # dashboard 模式下 issue 已经在跑了, 即使 updated_at 变了也不重新触发
                # (避免用户编辑 issue body 后又被 spawn 一个 window)
                continue
            elif seen_entry.get("updated_at") != updated_at:
                updated_items.append(item)
        return new_items, updated_items

    def _write_pending_file(self, issue: dict[str, Any], comments: list[dict[str, Any]]) -> Path:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        repo = issue["repository"]["full_name"]
        slug = repo.replace("/", "-").lower()
        target = self.pending_dir / f"{slug}-{issue['number']}.md"
        data = {
            "repo": repo,
            "number": issue["number"],
            "title": issue.get("title", ""),
            "author": (issue.get("user") or {}).get("login", ""),
            "state": issue.get("state", ""),
            "updated_at": issue.get("updated_at", ""),
            "url": issue.get("html_url", ""),
            "labels": [label.get("name", "") for label in issue.get("labels", [])],
        }
        lines = ["---", yaml.safe_dump(data, sort_keys=False).strip(), "---", "", "## Body", issue.get("body", ""), "", "## Comments"]
        for comment in comments:
            author = (comment.get("user") or {}).get("login", "unknown")
            created_at = comment.get("created_at", "")
            body = (comment.get("body") or "").replace("\n", " ").strip()
            lines.append(f"- {author} @ {created_at}: {body}")
        target.write_text("\n".join(lines) + "\n")
        return target

    def _dispatch_shenli(self, pending_file: Path) -> dict[str, Any]:
        cmd = os.environ.get("SHENLI_COMMAND")
        if cmd:
            argv = shlex.split(cmd) + ["--input", str(pending_file)]
        else:
            argv = [sys.executable, "-m", "shenli", "--input", str(pending_file)]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "shenli failed")
        return json.loads(proc.stdout)

    def _apply_decision(self, issue: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        repo = issue["repository"]["full_name"]
        number = int(issue["number"])
        action = decision["decision"]
        if action == "GO":
            active = self._count_active_issue_projects()
            if active >= self.max_concurrent_courts:
                retry_at = _iso_now()
                self.client.comment_on_issue(
                    repo,
                    number,
                    f"自动 court 数已达上限 {self.max_concurrent_courts}，当前延后重试。",
                )
                return {"last_action": "PENDING_RETRY", "retry_at": retry_at}

            plan = decision["agent_team_plan"]
            project_name = decision["court_project_name"]
            court_dir = self.court_root / "projects" / project_name
            temp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
            try:
                payload = {
                    "roles": plan["roles"],
                    "session": decision["session"],
                    "branch_prefix": decision["branch_prefix"],
                    "issue_ref": f"{repo}#{number}",
                }
                json.dump(payload, temp, ensure_ascii=False)
                temp.close()
                safe_env = self._safe_subprocess_env()
                migrate_cmd = [str(self.bin_dir / "migrate-to-court"), "--new", project_name, "--plan", temp.name, "--if-not-exists"]
                if court_dir.exists():
                    print(f"[gitea-watcher] reusing existing court project: {project_name}", file=sys.stderr)
                else:
                    subprocess.run(migrate_cmd, env=safe_env, check=True)
                court_env = self._safe_subprocess_env()
                court_env["COURT_UP_NO_ATTACH"] = "1"
                subprocess.run([str(self.bin_dir / "court-up"), project_name], env=court_env, check=True)
                server.dispatch_to_foreman(project_name, plan["dispatch_message"])
                self.client.comment_on_issue(
                    repo,
                    number,
                    f"已受理，court `{project_name}` 已创建并派发给 foreman。",
                )
                return {"last_action": "GO"}
            finally:
                try:
                    os.unlink(temp.name)
                except OSError:
                    pass
        if action == "NEED_INFO":
            self.client.comment_on_issue(repo, number, decision.get("comment_body") or "\n".join(decision.get("missing_info", [])))
            return {"last_action": "NEED_INFO"}
        if action == "REJECT":
            self.client.comment_on_issue(repo, number, decision.get("reject_reason", "自动审理判定拒绝。"))
            self.client.transition_issue(repo, number, "closed")
            return {"last_action": "REJECT"}
        raise ValueError(f"unknown decision: {action!r}")

    def _apply_decision_dashboard(self, issue: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        """dashboard 模式: 异步入站. GO 时只 queue 通知, 不等审批; spawn 由 ImReplyRouter 完成."""
        if decision["decision"] != "GO":
            return self._apply_decision(issue, decision)
        repo = issue["repository"]["full_name"]
        number = int(issue["number"])
        # 写完整 issue + decision 上下文到 pending-intake-context/, 让 router 之后 spawn 时能找到原始上下文
        ctx_dir = self.state_dir / "pending-intake-context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        slug = repo.replace("/", "-").lower()
        ctx_path = ctx_dir / f"{slug}-{number}.json"
        try:
            comments = self.client.list_issue_comments(repo, number)
        except GiteaClientError:
            comments = []
        ctx_payload = {"issue": issue, "decision": decision, "comments": comments}
        ctx_path.write_text(json.dumps(ctx_payload, ensure_ascii=False, indent=2))

        # 推送 IM + 终端提示, 立即 return, 不等审批
        approval = ApprovalStore(court_root=self.court_root)
        info = approval.queue_intake(repo, number, issue, decision)
        return {
            "last_action": "AWAITING_INTAKE_APPROVAL",
            "stage": "INTAKE",
            "intake_slug_id": info["slug_id"],
            "intake_msg_id": info["msg_id"],
        }

    def _safe_subprocess_env(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}

    def _drain_upstream_inboxes(self, seen: dict[str, Any]) -> None:
        projects_dir = self.court_root / "projects"
        if not projects_dir.exists():
            return
        by_project = {value.get("court_project"): value for value in seen.values() if value.get("court_project")}
        for project_dir in projects_dir.iterdir():
            inbox = project_dir / "bus" / "upstream" / "inbox"
            done_dir = inbox / ".done"
            if not inbox.is_dir():
                continue
            mapping = by_project.get(project_dir.name)
            if mapping is None:
                continue
            repo = mapping["repo"]
            number = int(mapping["number"])
            done_dir.mkdir(parents=True, exist_ok=True)
            for file in sorted(inbox.glob("*.md")):
                text = file.read_text().strip()
                if text:
                    self.client.comment_on_issue(repo, number, f"court 回执：\n\n{text}")
                shutil.move(str(file), str(done_dir / file.name))

    def _count_active_issue_projects(self) -> int:
        projects_dir = self.court_root / "projects"
        if not projects_dir.exists():
            return 0
        return sum(1 for path in projects_dir.iterdir() if path.is_dir() and path.name.startswith("issue-"))

    def _issue_key(self, issue: dict[str, Any]) -> str:
        return f"{self._issue_repo(issue)}#{issue['number']}"

    @staticmethod
    def _issue_repo(issue: dict[str, Any]) -> str:
        repo = issue.get("repository") or {}
        return repo.get("full_name") or issue.get("repository_url", "").rstrip("/").split("/repos/")[-1]

    def _ensure_dirs(self) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state_lock(self):
        return _FileLock(self._lock_path)

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, path)

    def _reset_errors(self) -> None:
        self._atomic_write_json(self.error_path, {"consecutive_failures": 0, "last_error": "", "updated_at": _iso_now()})

    def record_error(self, exc: Exception) -> int:
        state = self._load_json(self.error_path, {"consecutive_failures": 0})
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        state["last_error"] = str(exc)
        state["updated_at"] = _iso_now()
        self._atomic_write_json(self.error_path, state)
        if isinstance(exc, GiteaAuthError):
            subprocess.run(["osascript", "-e", 'display notification "Gitea token failed" with title "agent-court"'], check=False)
            return 78
        if isinstance(exc, GiteaRateLimitError):
            time.sleep(min(self.poll_interval, 5))
            return 75
        if isinstance(exc, (GiteaServerError, GiteaTransportError)) and state["consecutive_failures"] >= 5:
            subprocess.run(["osascript", "-e", 'display notification "Gitea watcher temp failure" with title "agent-court"'], check=False)
            return 75
        return 1


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.handle is not None
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m gitea_watcher")
    sub = parser.add_subparsers(dest="command", required=True)
    run_once = sub.add_parser("run-once")
    run_once.add_argument("--mode", default=os.environ.get("WATCHER_MODE", "court"))
    p = sub.add_parser("loop")
    p.add_argument("--poll-interval", type=int, default=30)
    p.add_argument("--mode", default=os.environ.get("WATCHER_MODE", "court"))
    status = sub.add_parser("status")
    status.add_argument("--mode", default=os.environ.get("WATCHER_MODE", "court"))
    args = parser.parse_args()
    watcher = GiteaWatcher(poll_interval=getattr(args, "poll_interval", 30), mode=getattr(args, "mode", "court"))
    try:
        if args.command == "run-once":
            print(json.dumps(watcher.run_once(), ensure_ascii=False, indent=2))
        elif args.command == "status":
            seen = watcher._load_json(watcher.seen_path, {})
            print("repo\tnum\tstage\twinner\ttmux_window")
            for row in seen.values():
                print(f"{row.get('repo','')}\t{row.get('number','')}\t{row.get('stage', row.get('last_action',''))}\t{row.get('approval_winner','')}\t{row.get('tmux_window','')}")
        else:
            watcher.loop()
    except (GiteaClientError, RuntimeError, ValueError) as exc:
        return watcher.record_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
