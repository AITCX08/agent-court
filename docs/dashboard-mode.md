# Dashboard 模式

## 什么是 dashboard 模式

`dashboard` 是 `gitea-watcher` 的**前台 tmux + 异步审批**模式：每个指派给我的 issue 被抓到后，先走双通道审批（终端 + 微信/飞书），任一通道 approve 即在 tmux session 里 spawn 一个独立 Claude CLI window 处理。

跟默认 `court` 模式的关系：

| 维度 | court (默认, PR-12) | dashboard (PR-13) |
|------|--------|--------|
| 抓到 issue 后 | shenli GO → migrate-to-court 建一座 court project (多 role 多 tmux 窗口黑盒协作) | shenli GO → 双通道等审批 → 起一个独立 Claude CLI tmux window |
| watcher 主循环 | 每 30s 跑一次 `run-once` (launchd 后台) | 前台 tmux session 长跑 + IM router 异步处理审批 |
| 多 issue 并发 | 受 MAX_CONCURRENT_COURTS 限制 | 异步消息驱动, watcher 主循环不阻塞 |
| 决策粒度 | 全自动 (shenli + court agent team 跑完) | 半自动 (用户在 intake / plan 两次审批) |
| commit/push | court 内部 dispatch (含 pre-push hook) | 同一套 pre-push hook (PR-12 复用) |

## 启动

```bash
# 第一次跑前: 确认凭证在 macOS Keychain (PR-12 已实现)
printf "protocol=https\nhost=git.k2lab.ai\n\n" | git credential-osxkeychain get

# 起一个前台 tmux session 跑 dashboard 模式
bin/gitea-watcher dashboard
```

启动后会自动 `tmux attach -t agent-court-dashboard`：
- window 0 (`watcher`) — watcher loop 实时 stdout
- window 1+ (`<repo-slug>-<num>`) — 收到 approve 后 router 自动 spawn 的 Claude CLI window

如果想跑后台 launchd 形式：

```bash
bin/gitea-watcher install --mode dashboard   # 生成 ~/Library/LaunchAgents/ai.k2lab.gitea-watcher.plist 含 WATCHER_MODE=dashboard
bin/gitea-watcher start                       # launchctl load
bin/gitea-watcher status                      # 看在不在
```

## 双通道审批机制

两个审批时机：
- **时机 ① intake**：watcher 抓到新 issue + shenli 判 GO → 立即推送 IM + 控制塔提示 → 等审批 → 任一通道 approve 即 spawn 新 window
- **时机 ② plan**：Claude window 内 shenli 复核 + 出 plan → 调 `python -m issue_resolver request-plan` → 又推 IM + 终端 → 等审批 → approve 即继续实施

任一通道先到先生效（fcntl.flock 仲裁），同毫秒 race 时终端优先。

**审批关键字**：

| 通道 | approve | 修改 | reject |
|------|---------|------|--------|
| 终端 | `可以` | `改 <修改意见>` | `拒 <原因>` |
| 微信 / 飞书 | `可以` | （不支持，需切终端）| `拒 <原因>` |

> 注: IM 收到 `改 <...>` reply 当前不识别（PR-13 范围），需要修改 plan 时请在终端 window 里直接说。后续 PR-14 可能扩展。

## 审批面板示例

终端 (window 0 `watcher`)：

```text
[approval] K2Lab/moras-finder#7 stage=INTAKE
title: 加批量导出按钮
url: https://git.k2lab.ai/K2Lab/moras-finder/issues/7
shenli: GO project=issue-k2lab-moras-finder-7
reply 可以 / 改 <instruction> / 拒 <reason>
> 
```

IM (微信/飞书)：

```text
[approval] K2Lab/moras-finder#7 stage=INTAKE
title: 加批量导出按钮
url: https://git.k2lab.ai/K2Lab/moras-finder/issues/7
shenli: GO project=issue-k2lab-moras-finder-7
reply 可以 / 拒 <reason>
```

approve 后 60s 内 `agent-court-dashboard` session 多一个 window `K2Lab-moras-finder-7`，window 里 Claude CLI 启动，加载 `.claude/skills/issue-resolver/SKILL.md`。

## 切换 window 观察

```bash
tmux attach -t agent-court-dashboard       # 进 session
# Ctrl-b 0   → watcher (主控制塔)
# Ctrl-b 1   → 第 1 个 issue window
# Ctrl-b 2   → 第 2 个 issue window
# Ctrl-b d   → 脱离 session 但保持后台跑
```

## 状态机

`seen-issues.json` 在 dashboard 模式下新增 `last_action` 值：

- `AWAITING_INTAKE_APPROVAL` — watcher 推 IM/控制塔，等用户审批 (intake 阶段)
- `DISPATCHED_DASHBOARD` — router 收到 approve，已 spawn window
- `EXECUTING` — Claude 在实施 plan
- `DONE_DASHBOARD` — issue 处理完成，window 可 kill
- `REJECTED_DASHBOARD` — 用户审批拒绝，issue 已 close
- `SPAWN_FAILED` — router 调 `spawn-issue-window` 失败 (查日志)

每个 entry 还会带 `approval_winner` (terminal/feishu/wechat) / `tmux_window` / `dispatched_at`.

查看：

```bash
cd mcp/court-mcp && .venv/bin/python -m gitea_watcher status --mode dashboard
```

## 安全栅栏（沿用 PR-12）

dashboard window 里 Claude CLI 跑完后 `git push` 走 PR-12 的 pre-push hook，**强制**校验：

- 分支前缀必须 `auto/issue-<num>/...`
- 禁 `--force` / `--force-with-lease`
- 禁 push `main`
- 每个 commit message 必须含 trailer `Issue: <repo>#<num>`

hook 安装到目标 repo 的 `.git/hooks/pre-push`，user 不要 `--no-verify` 绕过。

## 故障排查

| 症状 | 排查 |
|------|------|
| `bin/gitea-watcher dashboard` 起来 watcher 报 401 | macOS Keychain 里 git.k2lab.ai token 过期; 重新登录 / `git credential-osxkeychain erase` 后重 push 一次 |
| IM 没收到通知 | `shenpi_channels/` 配置缺失? 检查 `~/.agent-court/projects/gitea-watcher/court.yaml` 的 channels 段 |
| approve 后没起 window | 查 `~/.agent-court/gitea-watcher/pending-approval/.processed/` 是否有 `*.approve.json` (router 处理过了); `seen-issues.json` 是否 `SPAWN_FAILED` |
| window 里 Claude 立刻退出 | 你装的 `claude` CLI 不支持 `--append-system-prompt`? 用 PR-13 的 wrapper 看是否走到 `claude` 命令 (`tmux capture-pane -t agent-court-dashboard:<window>`) |
| pending-approval 文件堆积 | 长期未处理的 stale request; 用户人工删除 `~/.agent-court/gitea-watcher/pending-approval/*.json` 跟 `.lock` |
| 终端输错关键字被拒 | 重试即可 (PR-13 W3: ValueError 已被吞掉, 只提示重试) |
| router 线程死 | 进程 `tmux capture-pane -t agent-court-dashboard:watcher` 查 stderr; router `_run()` 有 try/except 兜底, 单条 result 失败不会杀线程 |
| issue 反复被处理 | `_diff` dashboard 守卫: DISPATCHED/EXECUTING/AWAITING 已在跑就不重 spawn; 若仍重触发, 检查 last_action 是否被外部脚本改成了别的值 |

## 切换回 court 模式

默认仍是 `court`。任意时刻：

```bash
bin/gitea-watcher install --mode court  # 重写 plist
bin/gitea-watcher --once                # 一次性 court 模式跑 (PR-12 行为不变)
```

`court` 模式 0 改动，PR-12 既有功能完整保留。

## 内部架构（开发者向）

```
┌────────────────────────────────────────────────────────────┐
│ tmux session: agent-court-dashboard                        │
│                                                            │
│ window 0 [watcher]                                         │
│   gitea_watcher.loop --mode dashboard                      │
│   ├─ 每 30s 调 list_assigned_issues                        │
│   ├─ _diff + shenli (跟 PR-12 共用)                        │
│   ├─ _apply_decision_dashboard:                            │
│   │    1. 写 pending-intake-context/<slug>-<num>.json      │
│   │    2. ApprovalStore.queue_intake (写 pending + 推 IM)  │
│   │    3. 立即 return (不阻塞)                              │
│   └─ ImReplyRouter 后台线程 (start 时拉起)                 │
│                                                            │
│ window 1+ [<repo-slug>-<num>]                              │
│   bin/spawn-issue-window 启动:                              │
│   - 通过 wrapper 用 bash 原生 $(<file) 读 SKILL.md (无注入) │
│   - exec </dev/tty 保持 stdin 不被 EOF 杀                  │
│   - Claude CLI 加载 issue-resolver skill                   │
│   - Claude 内部调 python -m issue_resolver request-plan    │
│     等 plan 审批 (阻塞读 _wait_for_result, 内部 drain IM)  │
└────────────────────────────────────────────────────────────┘

ImReplyRouter (后台线程):
  ┌─ 每 1s 扫 pending-approval/*-intake.result
  │  approve → 调 bin/spawn-issue-window + 更新 seen (DISPATCHED_DASHBOARD)
  │  reject  → comment_on_issue + transition_issue(closed) + 更新 seen
  │  archive .result 到 pending-approval/.processed/
  └─ PLAN result 不处理 (plan 由 request-plan 内部 drain)
```

## 文件清单（PR-13 落地）

- `mcp/court-mcp/dashboard_tmux.py` — tmux session/window/send-keys 转义
- `mcp/court-mcp/dual_channel_approval.py` — 审批仲裁 (fcntl.flock + 终端 stdin + IM 桥)
- `mcp/court-mcp/issue_resolver.py` — intro 消息 + request-plan/report-back CLI
- `mcp/court-mcp/im_reply_router.py` — INTAKE result 监听 → spawn / reject
- `mcp/court-mcp/seen_state.py` — seen-issues.json 共享锁 helper (watcher + resolver + router 共用)
- `bin/spawn-issue-window` — wrapper 启动 Claude CLI (stdin keep-open)
- `.claude/skills/issue-resolver/SKILL.md` — Claude window 内的完整流程 skill
