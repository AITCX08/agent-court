from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REJECT_LABELS = {"wontfix", "duplicate", "out-of-scope"}
ACCEPTANCE_RE = re.compile(r"(验收|完成定义|如何验证|acceptance)", re.IGNORECASE)
# TODO(PR-13): keep current rules-based screening, then add an LLM semantic
# review hook once the issue-driven flow is stable in production.


@dataclass
class PendingIssue:
    repo: str
    number: int
    title: str
    author: str
    state: str
    updated_at: str
    url: str
    labels: list[str]
    body: str
    comments: list[str]


def parse_pending_issue(path: Path) -> PendingIssue:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"invalid pending issue file: {path}")
    _, fm, rest = text.split("---\n", 2)
    meta = yaml.safe_load(fm) or {}
    body_block = rest.strip()
    issue_body = ""
    comments: list[str] = []
    if "## Body" in body_block:
        after_body = body_block.split("## Body", 1)[1]
        if "## Comments" in after_body:
            issue_body, comments_block = after_body.split("## Comments", 1)
            comments = [line.strip()[2:] for line in comments_block.splitlines() if line.strip().startswith("- ")]
        else:
            issue_body = after_body
    return PendingIssue(
        repo=str(meta.get("repo", "")),
        number=int(meta.get("number", 0)),
        title=str(meta.get("title", "")),
        author=str(meta.get("author", "")),
        state=str(meta.get("state", "")),
        updated_at=str(meta.get("updated_at", "")),
        url=str(meta.get("url", "")),
        labels=list(meta.get("labels", []) or []),
        body=issue_body.strip(),
        comments=comments,
    )


def repo_slug(repo: str) -> str:
    return repo.replace("/", "-").lower()


def local_repo_path(repo: str) -> Path:
    base = Path.home() / "Desktop" / "K2Work"
    name = repo.split("/", 1)[1] if "/" in repo else repo
    return base / name


def build_dispatch_message(issue: PendingIssue) -> str:
    return "\n".join(
        [
            f"处理 Gitea issue #{issue.number}: {issue.title}",
            f"Issue URL: {issue.url}",
            "",
            "严格执行以下 git 安全栅栏：",
            f"1. 只能在 `auto/issue-{issue.number}/...` 前缀分支上工作。",
            "2. 严禁 force push。",
            "3. 严禁修改或直接推送 `main`。",
            "4. push 前必须先 `git pull --rebase`。",
            f"5. 每个 commit message 必须以 `Issue: {issue.repo}#{issue.number}` 结尾。",
            "",
            "先阅读 issue，拆分实现与验证步骤，再通过 bus 协作。",
            "**git push 会被项目本地 pre-push hook 强制校验，请勿尝试绕过**",
            "",
            "Issue 正文：",
            issue.body.strip(),
        ]
    ).strip()


def build_plan(issue: PendingIssue) -> dict[str, Any]:
    labels = {label.lower() for label in issue.labels}
    work_dir = str(local_repo_path(issue.repo))
    roles: list[dict[str, Any]] = [
        {"name": "foreman", "cli": "claude", "model": "sonnet-4.6", "work_dir": work_dir},
        {"name": "dev", "cli": "claude", "model": "sonnet-4.6", "work_dir": work_dir},
        {"name": "qa", "cli": "claude", "model": "sonnet-4.6", "work_dir": work_dir},
    ]
    lower = f"{issue.title}\n{issue.body}".lower()
    if "frontend" in labels:
        roles.append({"name": "dev-frontend", "cli": "claude", "model": "sonnet-4.6", "work_dir": work_dir})
    if "backend" in labels or any(word in lower for word in ["flask", "express", "gin", "api", "database"]):
        roles.append({"name": "dev-backend", "cli": "claude", "model": "sonnet-4.6", "work_dir": work_dir})
    return {
        "decision": "GO",
        "court_project_name": f"issue-{repo_slug(issue.repo)}-{issue.number}",
        "session": f"agent-court-issue-{repo_slug(issue.repo)}-{issue.number}",
        "branch_prefix": f"auto/issue-{issue.number}/",
        "issue_ref": f"{issue.repo}#{issue.number}",
        "agent_team_plan": {
            "roles": roles,
            "dispatch_message": build_dispatch_message(issue),
        },
    }


def decide(issue: PendingIssue) -> dict[str, Any]:
    labels = {label.lower() for label in issue.labels}
    if labels & REJECT_LABELS:
        return {
            "decision": "REJECT",
            "court_project_name": f"issue-{repo_slug(issue.repo)}-{issue.number}",
            "reject_reason": f"issue labels contain reject markers: {sorted(labels & REJECT_LABELS)}",
        }

    missing: list[str] = []
    if not issue.title.strip():
        missing.append("缺少标题")
    if not issue.body.strip():
        missing.append("缺少 issue 正文")
    if "/" not in issue.repo:
        missing.append("缺少仓库归属（owner/name）")
    if not ACCEPTANCE_RE.search(issue.body):
        missing.append("缺少验收标准（例如“如何验证 / 验收 / acceptance”）")
    if not local_repo_path(issue.repo).exists():
        missing.append(f"本机未发现仓库克隆：{local_repo_path(issue.repo)}")

    if missing:
        return {
            "decision": "NEED_INFO",
            "court_project_name": f"issue-{repo_slug(issue.repo)}-{issue.number}",
            "missing_info": missing,
            "comment_body": "\n".join(
                [
                    "需要补充以下信息后才能自动派活：",
                    *[f"- {item}" for item in missing],
                ]
            ),
        }

    result = build_plan(issue)
    result["agent_team_plan"]["dispatch_message"] = build_dispatch_message(issue)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m shenli")
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    issue = parse_pending_issue(Path(args.input))
    print(json.dumps(decide(issue), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
