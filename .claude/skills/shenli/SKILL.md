---
name: shenli
description: 审理 Gitea issue，输出 GO / NEED_INFO / REJECT JSON，并给出 court agent team plan。
when_to_use: 当 watcher 或人工需要对 pending issue 做准入判断时。
trigger_keywords:
  - shenli
  - issue-driven
  - 审理 issue
---

# shenli

输入：`pending-shenli/<repo>-<num>.md`

输出：严格 JSON，由 `python -m shenli --input <file>` 生成。

## 决策流程

1. 检查标签：若存在 `wontfix` / `duplicate` / `out-of-scope`，直接 `REJECT`
2. 检查字段：标题、repo、body 不能为空
3. 检查验收标准：正文需出现 `验收` / `完成定义` / `如何验证` / `acceptance`
4. 检查本机仓库：默认要求 `~/Desktop/K2Work/<repo_name>` 已存在
5. 满足条件则 `GO`，并生成 agent team plan

## agent_team_plan 生成规则

- 任何 `GO` 至少包含 `foreman` + `dev` + `qa`
- 标签含 `frontend` 时增加 `dev-frontend`
- 标签含 `backend`，或正文提到 `Flask` / `Express` / `Gin` / `API` / `database` 时增加 `dev-backend`
- `dispatch_message` 必须包含以下安全栅栏：
  - 分支前缀只能是 `auto/issue-<num>/`
  - 禁止 force push
  - 禁止修改 `main`
  - push 前先 `git pull --rebase`
  - 每个 commit message 结尾必须带 `Issue: <repo>#<num>`

## 后续动作

- `GO`:
  - `bin/migrate-to-court --new <court_project_name> --plan <plan.json>`
  - `COURT_UP_NO_ATTACH=1 bin/court-up <court_project_name>`
  - 通过既有 `dispatch_to_foreman(...)` 派发 `dispatch_message`
- `NEED_INFO`:
  - `python -m gitea_client comment --repo <repo> --num <num> --body "<missing_info>"`
- `REJECT`:
  - `python -m gitea_client comment --repo <repo> --num <num> --body "<reject_reason>"`
  - `python -m gitea_client transition --repo <repo> --num <num> --state closed`
