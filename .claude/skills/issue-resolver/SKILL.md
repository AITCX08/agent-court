---
name: issue-resolver
description: 处理单个 Gitea issue 的完整闭环 skill,由 dashboard 模式 spawn 的独立 Claude CLI 使用.
trigger: stdin 第一行收到 `ISSUE_RESOLVER_BEGIN <repo> <num>` 标记
---

# issue-resolver

你被 agent-court dashboard 模式 spawn,负责处理**一个**指派给我的 Gitea issue 全闭环.

## 你收到的输入 (stdin)

stdin 第一段是结构化的 issue 上下文,以 `ISSUE_RESOLVER_BEGIN <repo> <num>` 开头.
包含三段 JSON:

```
ISSUE_RESOLVER_BEGIN <repo> <num>
--- issue.json ---
{title, body, labels, html_url, repository.full_name, ...}
--- comments.json ---
[{user.login, body, created_at}, ...]
--- shenli.decision.json ---
{decision: "GO", court_project_name, branch_prefix, agent_team_plan, ...}
```

之后 stdin 切到 tty (用户终端) — 用户在 tmux window 里跟你交互.

## 工作流程

### 1. 审需求

读 issue.json + comments.json + shenli.decision.json.shenli 已经判 GO,你**必要时**才重跑一次复核.若 issue body 在 watcher 抓到之后被人改过且偏离原意,可以拒绝 (走步骤 5 拒绝路径).

### 2. 出实施 plan

写一份 markdown plan 到临时文件 (如 `/tmp/plan-<repo-slug>-<num>.md`),内容至少包含:

```markdown
## 目标
<一句话目标>

## 改动范围
- 文件 / 模块清单

## 步骤
1. ...
2. ...

## 风险
- ...

## 验收
- [ ] 测试通过
- [ ] ...
```

### 3. 二次审批 (阻塞)

调用:
```bash
python -m issue_resolver request-plan \
  --repo <repo> --num <num> \
  --plan-file /tmp/plan-<repo-slug>-<num>.md \
  --window <tmux-window-name>
```

这条命令会**阻塞**直到终端或 IM (微信/飞书) 回复审批结论.返回的 JSON 形如:

```json
{"approved": true, "winner": "terminal|wechat|feishu", "reason": "", "edit_instruction": ""}
```

- `approved=true` → 进入步骤 4 实施
- `approved=false` 且 `edit_instruction` 非空 → 按 edit_instruction 修改 plan,**回到步骤 3 重新发审批**
- `approved=false` 且 `edit_instruction` 空 → 步骤 5 拒绝路径

### 4. 实施

在 issue 对应的本地仓库工作.路径默认是 `~/Desktop/K2Work/<repo_name>/` (从 repo full_name 推断,如 `K2Lab/moras-finder` → `~/Desktop/K2Work/moras-finder/`).

约束 (pre-push hook 强制,**绝对绕不过**):

- **分支前缀必须** `auto/issue-<num>/<short-summary>` (如 `auto/issue-7/add-export-button`)
- **禁止** push 到 `main`
- **禁止** `git push --force` / `--force-with-lease`
- **每个 commit** 的 message **必须**带 trailer:
  ```
  Issue: <repo>#<num>
  ```
  如 `Issue: K2Lab/moras-finder#7`

实施完先调一次 report-back 标 EXECUTING:
```bash
python -m issue_resolver report-back \
  --repo <repo> --num <num> \
  --summary-file /tmp/intermediate-<num>.txt \
  --stage executing \
  --no-comment
```

### 5. 提交 + 推送

按 conventional commits 风格 commit,trailer 必填.例:

```
feat(export): add batch export button to product list

实现批量导出当前筛选商品到 xlsx, 关联 issue 验收点.

Issue: K2Lab/moras-finder#7
```

push 时若 pre-push hook 拒绝:

- 看 hook stderr 找原因 (常见: 分支不是 auto/issue-<N>/ 前缀; commit 缺 trailer)
- **不要** `--no-verify` 绕过
- 改完 + amend commit + 重新 push

### 6. 完成回写

```bash
python -m issue_resolver report-back \
  --repo <repo> --num <num> \
  --summary-file /tmp/summary-<num>.txt \
  --stage done
```

`--stage done` 会:
- 更新 seen-issues.json `last_action=DONE_DASHBOARD`
- 自动在 Gitea issue 上发完成评论 (含 winner / summary)

### 7. 拒绝路径 (用户审批拒了)

如果 step 3 收到 `approved=false` 且 `edit_instruction` 空,意味着用户决定不做.动作:

1. 调 `GiteaClient.comment_on_issue(repo, num, "<reason>")` 写明拒绝原因
2. 调 `GiteaClient.transition_issue(repo, num, "closed")` 关 issue
3. 调 `python -m issue_resolver report-back --stage done --summary-file <reason>.txt --no-comment` 标完成

或者直接退出 (`exit` / Ctrl-D),由 router 兜底.

## 失败恢复

- **push 失败 + 改不动 hook**: 在 issue 上评论详情 + 调 report-back 标 done (但 last_action=PUSH_FAILED 由 helper 决定),等待人工
- **找不到本地仓库**: 评论 issue "找不到 `~/Desktop/K2Work/<repo>`, 需手动 git clone",标 done
- **测试不通过**: 把失败 log 摘要到 issue 评论,标 done
- **整体卡 30 分钟无进展**: 用户可以在 tmux window 里直接 `Ctrl-C` 中断,你应在中断后善后

## IM 通道限制

- 微信 / 飞书审批**只支持 approve / reject** (不支持 `edit:<修改>`)
- 用户在 IM 想改 plan 时,必须切到终端 (tmux attach 到 dashboard session → 切到 window) 直接说

## 关键不变量

1. 不修改 `bus/` 协议,本 skill 是独立 Claude window 流不依赖 court 多 role
2. 完成前不关 tmux window (用户可能要审 commit / push)
3. 所有 commit 必带 Issue trailer (pre-push hook 校验)
