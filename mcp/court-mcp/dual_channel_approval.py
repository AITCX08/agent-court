from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import select
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

import bangjiao
import shenpi


DEFAULT_PROJECT = "gitea-watcher"
DEFAULT_PEER = "dashboard"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Verdict:
    approved: bool
    winner: str
    reason: str = ""
    edit_instruction: str = ""


class ApprovalStore:
    def __init__(self, court_root: Path | None = None) -> None:
        self.court_root = court_root or Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court")))
        self.pending_dir = self.court_root / "gitea-watcher" / "pending-approval"
        self.project_dir = self.court_root / "projects" / DEFAULT_PROJECT

    def _slug_id(self, repo: str, num: int, stage: str) -> str:
        digest = sha1(f"{repo}#{num}:{stage}".encode("utf-8")).hexdigest()[:12]
        return f"{repo.replace('/', '-').lower()}-{int(num)}-{stage.lower()}", digest

    def _paths(self, slug_id: str) -> dict[str, Path]:
        return {
            "json": self.pending_dir / f"{slug_id}.json",
            "lock": self.pending_dir / f"{slug_id}.lock",
            "result": self.pending_dir / f"{slug_id}.result",
        }

    def request_intake(self, repo: str, num: int, issue_detail: dict[str, Any], shenli_decision: dict[str, Any]) -> Verdict:
        terminal_body = self._build_intake_body(repo, num, issue_detail, shenli_decision, im=False)
        im_body = self._build_intake_body(repo, num, issue_detail, shenli_decision, im=True)
        return self._request(stage="INTAKE", repo=repo, num=num, terminal_body=terminal_body, im_body=im_body, payload={"issue": issue_detail, "decision": shenli_decision})

    def queue_intake(self, repo: str, num: int, issue_detail: dict[str, Any], shenli_decision: dict[str, Any]) -> dict[str, Any]:
        """非阻塞:写 pending + 推送 IM,立即 return.审批结果由 ImReplyRouter 处理."""
        terminal_body = self._build_intake_body(repo, num, issue_detail, shenli_decision, im=False)
        im_body = self._build_intake_body(repo, num, issue_detail, shenli_decision, im=True)
        return self._queue(stage="INTAKE", repo=repo, num=num, terminal_body=terminal_body, im_body=im_body, payload={"issue": issue_detail, "decision": shenli_decision})

    def request_plan(self, repo: str, num: int, plan_text: str, window_name: str) -> Verdict:
        terminal_body = self._build_plan_body(repo, num, plan_text, window_name, im=False)
        im_body = self._build_plan_body(repo, num, plan_text, window_name, im=True)
        return self._request(stage="PLAN", repo=repo, num=num, terminal_body=terminal_body, im_body=im_body, payload={"plan_text": plan_text, "tmux_window": window_name})

    def _queue(self, *, stage: str, repo: str, num: int, terminal_body: str, im_body: str, payload: dict[str, Any]) -> dict[str, Any]:
        """写 pending 文件 + 推送 IM/终端 提示,不等审批,return slug_id 让调用方记到 seen-issues.json."""
        slug_id, msg_id = self._slug_id(repo, num, stage)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "stage": stage,
            "repo": repo,
            "number": int(num),
            "slug_id": slug_id,
            "msg_id": msg_id,
            "payload": payload,
            "created_at": _iso_now(),
            "channels": self._channels(),
        }
        paths = self._paths(slug_id)
        paths["json"].write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        self._ensure_shenpi_project()
        pending_item = self._write_shenpi_pending(meta, im_body)
        print(self._terminal_prompt(meta, terminal_body), file=sys.stderr, flush=True)
        self._notify_im(pending_item)
        return {"slug_id": slug_id, "msg_id": msg_id, "pending_file": str(pending_item.filepath)}

    def _request(self, *, stage: str, repo: str, num: int, terminal_body: str, im_body: str, payload: dict[str, Any]) -> Verdict:
        meta_info = self._queue(stage=stage, repo=repo, num=num, terminal_body=terminal_body, im_body=im_body, payload=payload)
        meta = {
            "stage": stage,
            "repo": repo,
            "number": int(num),
            "slug_id": meta_info["slug_id"],
            "msg_id": meta_info["msg_id"],
        }
        paths = self._paths(meta_info["slug_id"])
        result = self._wait_for_result(meta, timeout_seconds=86400)
        self._cleanup(paths, Path(meta_info["pending_file"]))
        return Verdict(
            approved=result["verdict"] == "approve",
            winner=result.get("winner", "terminal"),
            reason=result.get("reason", ""),
            edit_instruction=result.get("edit_instruction", ""),
        )

    def submit_verdict(
        self,
        repo: str,
        num: int,
        *,
        stage: str,
        verdict: str,
        winner: str,
        reason: str = "",
        edit_instruction: str = "",
        grace_seconds: float = 0.0,
    ) -> bool:
        slug_id, _ = self._slug_id(repo, num, stage)
        paths = self._paths(slug_id)
        if grace_seconds > 0:
            time.sleep(grace_seconds)
        paths["lock"].parent.mkdir(parents=True, exist_ok=True)
        with paths["lock"].open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            if paths["result"].exists():
                return False
            payload = {
                "repo": repo,
                "number": int(num),
                "stage": stage,
                "verdict": verdict,
                "winner": winner,
                "reason": reason,
                "edit_instruction": edit_instruction,
                "at": _iso_now(),
            }
            # W2: tempfile + os.replace 原子写, 避免 router 读到半写状态
            import tempfile
            with tempfile.NamedTemporaryFile(
                "w",
                dir=paths["result"].parent,
                prefix=f".{paths['result'].stem}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_handle:
                json.dump(payload, tmp_handle, ensure_ascii=False, indent=2)
                tmp_handle.flush()
                os.fsync(tmp_handle.fileno())
                temp_name = tmp_handle.name
            os.replace(temp_name, paths["result"])
            return True

    def _wait_for_result(self, meta: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        slug_id = meta["slug_id"]
        started = time.monotonic()
        paths = self._paths(slug_id)
        while True:
            if paths["result"].exists():
                return json.loads(paths["result"].read_text())
            self._drain_shenpi_bus(meta)
            if paths["result"].exists():
                return json.loads(paths["result"].read_text())
            if sys.stdin and not sys.stdin.closed:
                readable, _, _ = select.select([sys.stdin], [], [], 0.25)
                if readable:
                    line = sys.stdin.readline()
                    if line:
                        self._handle_terminal_line(meta, line.rstrip("\n"))
                        continue
            else:
                time.sleep(0.25)
            if time.monotonic() - started > timeout_seconds:
                self.submit_verdict(meta["repo"], meta["number"], stage=meta["stage"], verdict="reject", winner="system", reason="approval timeout")
                return json.loads(paths["result"].read_text())

    def _handle_terminal_line(self, meta: dict[str, Any], line: str) -> None:
        try:
            verdict, reason, edit_instruction = _parse_terminal_reply(line)
        except ValueError as exc:
            print(f"[approval] {exc}; 请重试 (可以 / 改 <instruction> / 拒 <reason>)", file=sys.stderr, flush=True)
            return
        ok = self.submit_verdict(
            meta["repo"],
            meta["number"],
            stage=meta["stage"],
            verdict=verdict,
            winner="terminal",
            reason=reason,
            edit_instruction=edit_instruction,
        )
        if ok:
            print(f"[approval] terminal verdict={verdict} recorded", file=sys.stderr, flush=True)

    def _ensure_shenpi_project(self) -> None:
        for rel in [
            Path("bus") / DEFAULT_PEER / "pending-approval",
            Path("bus") / DEFAULT_PEER / "inbox",
            Path("bus") / DEFAULT_PEER / "denied",
            Path("logs"),
            Path("shared"),
        ]:
            (self.project_dir / rel).mkdir(parents=True, exist_ok=True)

    def _write_shenpi_pending(self, meta: dict[str, Any], body: str) -> shenpi.PendingItem:
        ts_epoch = int(datetime.now(timezone.utc).timestamp())
        file_path = (
            self.project_dir
            / "bus"
            / DEFAULT_PEER
            / "pending-approval"
            / f"{ts_epoch}-{meta['msg_id']}-dashboard-to-approver.md"
        )
        lines = [
            "---",
            f"id: {meta['msg_id']}",
            "from: dashboard",
            "to: approver",
            f"ts: {_iso_now()}",
            f"repo: {meta['repo']}",
            f"number: {meta['number']}",
            f"stage: {meta['stage']}",
            "---",
            "",
            body,
            "",
            f"repo={meta['repo']} number={meta['number']} stage={meta['stage']}",
        ]
        file_path.write_text("\n".join(lines) + "\n")
        item = shenpi._parse_file(DEFAULT_PROJECT, DEFAULT_PEER, file_path)
        if item is None:
            raise RuntimeError(f"failed to parse pending approval file: {file_path}")
        return item

    def _notify_im(self, item: shenpi.PendingItem) -> None:
        cfg = bangjiao.ShenpiConfig(enabled=True, channels=self._channels())
        try:
            asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[approval] notify failed: {exc}", file=sys.stderr, flush=True)

    def _drain_shenpi_bus(self, meta: dict[str, Any]) -> None:
        msg_id = meta["msg_id"]
        inbox = self.project_dir / "bus" / DEFAULT_PEER / "inbox"
        denied = self.project_dir / "bus" / DEFAULT_PEER / "denied"
        for folder, verdict in ((inbox, "approve"), (denied, "reject")):
            for path in sorted(folder.glob(f"*-{msg_id}-*.md")):
                winner = self._winner_from_audit(msg_id)
                self.submit_verdict(
                    meta["repo"],
                    meta["number"],
                    stage=meta["stage"],
                    verdict=verdict,
                    winner=winner,
                    reason="denied from IM" if verdict == "reject" else "",
                    grace_seconds=0.25,
                )
                done_dir = folder / ".done"
                done_dir.mkdir(parents=True, exist_ok=True)
                path.replace(done_dir / path.name)

    def _winner_from_audit(self, msg_id: str) -> str:
        audit = self.project_dir / "logs" / "approval-log.jsonl"
        if not audit.exists():
            return "terminal"
        for line in reversed(audit.read_text().splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("msg_id") != msg_id:
                continue
            actor = str(row.get("by") or "").lower()
            if "feishu" in actor:
                return "feishu"
            if "wechat" in actor:
                return "wechat"
            return "terminal"
        return "terminal"

    def _cleanup(self, paths: dict[str, Path], pending_file: Path) -> None:
        for path in [*paths.values(), pending_file]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _channels(self) -> list[str]:
        raw = os.environ.get("DASHBOARD_APPROVAL_CHANNELS", "terminal,feishu,wechat")
        channels = [item.strip() for item in raw.split(",") if item.strip()]
        return channels or ["terminal"]

    def _build_intake_body(self, repo: str, num: int, issue_detail: dict[str, Any], decision: dict[str, Any], *, im: bool = False) -> str:
        instructions = "reply 可以 / 拒 <reason>" if im else "reply 可以 / 改 <instruction> / 拒 <reason>"
        return "\n".join([
            f"[approval] {repo}#{num} stage=INTAKE",
            f"title: {issue_detail.get('title', '')}",
            f"url: {issue_detail.get('html_url', '')}",
            f"shenli: {decision.get('decision', '')} project={decision.get('court_project_name', '')}",
            instructions,
        ])

    def _build_plan_body(self, repo: str, num: int, plan_text: str, window_name: str, *, im: bool = False) -> str:
        instructions = "reply 可以 / 拒 <reason>" if im else "reply 可以 / 改 <instruction> / 拒 <reason>"
        return "\n".join([
            f"[approval] {repo}#{num} stage=PLAN",
            f"window: {window_name}",
            instructions,
            "",
            plan_text.strip(),
        ]).strip()

    def _terminal_prompt(self, meta: dict[str, Any], body: str) -> str:
        return f"{body}\n> "


def _parse_terminal_reply(text: str) -> tuple[str, str, str]:
    cleaned = text.strip()
    if cleaned == "可以":
        return "approve", "", ""
    if cleaned.startswith("改 "):
        return "edit", "", cleaned[2:].strip()
    if cleaned == "拒":
        return "reject", "rejected", ""
    if cleaned.startswith("拒 "):
        return "reject", cleaned[2:].strip(), ""
    raise ValueError(f"unsupported approval reply: {text!r}")


_DEFAULT_STORE = ApprovalStore()


def request_intake(repo: str, num: int, issue_detail: dict[str, Any], shenli_decision: dict[str, Any]) -> Verdict:
    return _DEFAULT_STORE.request_intake(repo, num, issue_detail, shenli_decision)


def request_plan(repo: str, num: int, plan_text: str, window_name: str) -> Verdict:
    return _DEFAULT_STORE.request_plan(repo, num, plan_text, window_name)


def submit_verdict(
    repo: str,
    num: int,
    *,
    stage: str,
    verdict: str,
    winner: str,
    reason: str = "",
    edit_instruction: str = "",
    grace_seconds: float = 0.0,
) -> bool:
    return _DEFAULT_STORE.submit_verdict(
        repo,
        num,
        stage=stage,
        verdict=verdict,
        winner=winner,
        reason=reason,
        edit_instruction=edit_instruction,
        grace_seconds=grace_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dual_channel_approval")
    sub = parser.add_subparsers(dest="command", required=True)

    request_plan_parser = sub.add_parser("request-plan")
    request_plan_parser.add_argument("--repo", required=True)
    request_plan_parser.add_argument("--num", required=True, type=int)
    request_plan_parser.add_argument("--plan-file", required=True)
    request_plan_parser.add_argument("--window", required=True)

    args = parser.parse_args(argv)
    if args.command == "request-plan":
        verdict = request_plan(args.repo, args.num, Path(args.plan_file).read_text(), args.window)
        print(json.dumps(verdict.__dict__, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
