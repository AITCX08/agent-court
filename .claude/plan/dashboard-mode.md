# PR-13 · Dashboard 模式 — WBS 规划

| 字段 | 值 |
|---|---|
| **PR 号** | PR-13 |
| **规划日期** | 2026-05-19 |
| **基线分支** | `feat/pr-13-dashboard-mode`（stacked on `feat/pr-12-issue-driven`，基于 commit `8369f0f`） |
| **依赖 PR** | PR-12（court 模式 / 状态机 / pre-push hook）· PR-5（pizhun / pending-approval 文件总线）· PR-6（shenpi_channels 三通道） |
| **任务总数** | 24（M1-M6 共 18 + 集成测试 2 + 文档 2 + 收尾 2） |
| **预估工时** | 8 人日（保守，含联调踩坑 buffer） |
| **关键风险数** | 10 条 |

---

## 1. 一句话目标

在 PR-12「watcher 抓 issue → 自动起 court」黑盒之外，新增一条 **dashboard 控制塔模式**：watcher 抓到 issue 后**先双通道审批**，approve 才 spawn 独立 Claude CLI tmux window 自跑 shenli + 出 plan + 二次审批 + 实施 + commit/push + 回 issue 评论，全程在 `tmux session agent-court-dashboard` 里可视。

---

## 2. 架构图（数据流）

```
                       ┌───────────────────────────────────────────────────────┐
                       │  Gitea (远端 issue 仓)                                  │
                       └────────────────┬──────────────────────────────────────┘
                                        │ list_assigned_issues
                                        ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  gitea_watcher.py  (--mode dashboard)                                  │
   │  ── 前台 loop, 跑在 tmux:agent-court-dashboard:0 (watcher window)     │
   │                                                                       │
   │  for issue in new+updated:                                            │
   │     ① write pending-approval/<id>.json  (stage=INTAKE)                │
   │     ② notify_terminal(stdout) + notify_im(channels)                   │
   │     ③ block on dual_channel_approval.wait_for_verdict()               │
   │            ▲                                                          │
   │            │ result.json 落地 → 解锁                                   │
   │     ④ approve → spawn_issue_window(repo, num)                         │
   │           tmux new-window -t agent-court-dashboard                    │
   │           → bin/spawn-issue-window <repo> <num>                       │
   │           → claude --append-system-prompt issue-resolver/SKILL.md     │
   │                                                                       │
   │     ⑤ Claude window 内自跑:                                            │
   │           读 issue → shenli → 出 plan → 写 pending-approval (PLAN) →  │
   │           等审批 → 实施 → commit/push → 评论 issue                     │
   └──────────────────┬─────────────────┬────────────────────────────────┘
                      │                 │
        notify_im     │                 │  IM reply (微信/飞书)
        ─────────────►│                 │  ─────────────────────►
                      ▼                 ▼
        ┌──────────────────────┐  ┌──────────────────────────────────┐
        │ shenpi_channels/     │  │ im_reply_router.py               │
        │  - wechat            │  │  watch pending-approval/*.json   │
        │  - feishu            │  │  result=approve →                 │
        │  - terminal (stdin)  │  │    if INTAKE: trigger spawn       │
        └──────────────────────┘  │    if PLAN:   tmux send-keys      │
                                  │       "可以" Enter into window     │
                                  └───────────────────────────────────┘

        ─── 仲裁锁 ────────────────────────────────────────────────────
          $COURT_ROOT/gitea-watcher/pending-approval/
             <repo>-<num>.json    ← 审批载荷
             <repo>-<num>.lock    ← fcntl.flock 排它
             <repo>-<num>.result  ← winner (terminal/wechat/feishu)
```

---

## 3. 关键设计决策（DR）

| # | 决策 | 一句话理由 |
|---|---|---|
| DR-1 | watcher 加 `--mode {court,dashboard}`，默认 court | PR-12 行为零变更，dashboard 完全可选 |
| DR-2 | 介质纯 tmux，不引入 Web UI/screen | 沿用 `bin/court-up` 既有 tmux 风格，0 新依赖 |
| DR-3 | 每 issue 一个独立 Claude CLI tmux window | 进程隔离 = 故障隔离 + 用户可 attach 看流程 |
| DR-4 | 双通道任一先到先得 | 用户可能在外（微信 reply）也可能在桌面（终端打），不强制单一入口 |
| DR-5 | 终端 race 优先 | 终端比 IM 延迟低 50-500ms，并发概率近 0，优先=简化语义 |
| DR-6 | 沿用 PR-5 的 pizhun/pibo 动词 + `pending-approval/` 路径 | 不发明新审批动词，复用 court-approve / shenpi_channels 直接可用 |
| DR-7 | 两次审批（intake / plan）独立 pending 文件，不共用 | 状态机简单，文件即状态 |
| DR-8 | issue-resolver skill 用 Anthropic frontmatter 格式 | 复用 .claude/skills/shenli/SKILL.md 既有约定 |
| DR-9 | tmux send-keys 注入审批信号（"可以\n"/"改 X\n"/"拒\n"），不开 RPC 端口 | 0 新协议，0 新端口，REPL 友好 |
| DR-10 | commit/push 完全复用 PR-12 pre-push hook（分支前缀 `auto/issue-<num>/`、禁 force、禁 main、commit trailer 必填） | 安全栅栏不动 = PR-12 测试用例继承 |
| DR-11 | seen-issues.json 新增 dashboard 状态，与 court 状态平行不冲突 | PR-12 court 状态机原样保留 |
| DR-12 | IM reply 入站仍走现有 court-approve / pizhun，不新建 webhook | PR-5/6 的 cc-connect → MCP pizhun 链路零改动 |

---

## 4. WBS 任务表

### 4.1 M1 · watcher 双模式分支（3 任务，1.5 人日）

#### T-13-01 · CLI 与环境变量解析 — 小

**目标**：让 `python -m gitea_watcher` 支持 `--mode court|dashboard` 与 `WATCHER_MODE` env 兜底。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_watcher.py`（修改 `main()` + `GiteaWatcher.__init__`）

**关键接口**：
```python
class GiteaWatcher:
    def __init__(self, ..., mode: str = "court"): ...
    # mode ∈ {"court", "dashboard"}; 非法值 raise ValueError
```
CLI：
```
python -m gitea_watcher loop --mode dashboard --poll-interval 30
python -m gitea_watcher run-once --mode dashboard
WATCHER_MODE=dashboard python -m gitea_watcher loop  # env 兜底
```

**依赖**：无（首发任务）
**验收**：`python -m gitea_watcher loop --mode dashboard --help` 显示新参数；`WATCHER_MODE=invalid` 立即报错退出码 ≠0。

---

#### T-13-02 · run_once 内分流到 court / dashboard 路径 — 中

**目标**：mode=court → 原 `_apply_decision` 流程不变；mode=dashboard → 新 `_apply_decision_dashboard` 分支（先写 pending-approval，等审批）。

**产出文件**：
- `gitea_watcher.py` 新增方法 `_apply_decision_dashboard(detail, decision)`，签名同 `_apply_decision`

**关键接口**：
```python
def _apply_decision_dashboard(self, issue, decision) -> dict:
    # decision 仍来自 shenli, 用于把 reject/need_info 直接走 PR-12 老逻辑
    if decision["decision"] != "GO":
        return self._apply_decision(issue, decision)   # 复用 PR-12
    # GO 分支: 走 dual_channel_approval intake
    verdict = dual_channel_approval.request_intake(repo, num, issue, decision)
    if verdict.approved:
        self._spawn_dashboard_window(repo, num, issue, decision, verdict.winner)
        return {"last_action": "DISPATCHED_DASHBOARD", "approval_winner": verdict.winner}
    return {"last_action": "REJECTED_DASHBOARD", "reject_reason": verdict.reason}
```

**依赖**：T-13-01 / M2-T-13-04 / M3-T-13-07
**验收**：`tests/test_watcher_mode_branch.py`：mock shenli 返回 GO → 验证 dashboard 走 `request_intake`，court 走 `_apply_decision`，互不串流。

---

#### T-13-03 · `bin/gitea-watcher dashboard` 子命令 + plist 加 env — 小

**目标**：CLI 入口加 `dashboard` 前台子命令；launchd plist 模板支持 `WATCHER_MODE` env。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/bin/gitea-watcher`（case 加 `dashboard)`）
- `/Users/wjx/Desktop/K2Work/agent-court/docs/launchd/ai.k2lab.gitea-watcher.plist.template`（加 `EnvironmentVariables.WATCHER_MODE`）

**关键接口**：
```bash
gitea-watcher dashboard          # 前台 loop, mode=dashboard, attach 当前终端
gitea-watcher --once             # 不变, 沿用 court 默认
WATCHER_MODE=dashboard gitea-watcher start   # launchd 起 dashboard
```

**依赖**：T-13-01
**验收**：`bin/gitea-watcher dashboard --help`（透传到 python）显示新模式；`grep WATCHER_MODE docs/launchd/*.template` 命中。

---

### 4.2 M2 · 控制塔 tmux session 管理（3 任务，1.5 人日）

#### T-13-04 · `dashboard_tmux.py` 模块骨架 — 中

**目标**：封装所有 tmux 操作，subprocess 调 `tmux` 命令，单测可 mock。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/dashboard_tmux.py`（新）

**关键接口**：
```python
SESSION_NAME = "agent-court-dashboard"
WATCHER_WINDOW = "watcher"

def session_exists() -> bool: ...
def ensure_session() -> None:
    """无则建, 有则跳过. 建时第一个 window 命名为 'watcher'."""

def issue_window_name(repo: str, num: int) -> str:
    """e.g. K2Lab-moras-finder-42 (强校验 tmux 合法字符)."""

def new_issue_window(repo: str, num: int, launch_cmd: str) -> str:
    """tmux new-window -t agent-court-dashboard -n <name>
       tmux send-keys -t <name> <launch_cmd> Enter
       return window_name"""

def window_exists(name: str) -> bool: ...
def list_windows() -> list[str]: ...

def inject(name: str, text: str, *, send_enter: bool = True) -> None:
    """tmux send-keys -t agent-court-dashboard:<name> -l <escaped_text>
       注意: -l 字面模式禁用控制字符插值, 单独 send 'Enter' 触发回车."""

def kill_window(name: str) -> None: ...
def attach() -> None:
    """阻塞调用, 给 bin/gitea-watcher dashboard 用."""
```

**依赖**：无（独立模块，可先做单测）
**验收**：`pytest tests/test_dashboard_tmux.py -v`：用 monkeypatch 替换 `subprocess.run`，验证生成的 tmux argv 正确（含正确的 -t target / -l 字面模式 / Enter 单独发）。

---

#### T-13-05 · tmux send-keys 转义工具 — 小

**目标**：处理 `;` / `$` / `"` / 控制字符在 tmux send-keys 下的转义陷阱，避免审批文本被 tmux 解析成命令分隔符。

**产出文件**：
- `dashboard_tmux.py` 内函数 `_safe_inject_text(text: str) -> str`

**关键接口**：
```python
def _safe_inject_text(text: str) -> str:
    # 1. 拒绝 \x00-\x08, \x0b-\x1f, \x7f (除 \n \t)
    # 2. tmux send-keys 用 -l (literal) 模式可绕开 ; 解析问题
    # 3. 多行: 拆成 [(line, send_enter=False), ..., (Enter, True)]
```

**依赖**：T-13-04
**验收**：单测覆盖 `";rm -rf /"`、`"改 X\n确认"`、`"bell"` 三类输入；前两者按字面注入，第三个拒绝。

---

#### T-13-06 · 启动入口集成（watcher 进 session window 0） — 小

**目标**：`gitea-watcher dashboard` 命令执行：① 确保 session 存在；② 把自己（python loop）跑在 `watcher` window 内；③ 用户后续可 `tmux attach -t agent-court-dashboard`。

**产出文件**：
- `bin/gitea-watcher`（dashboard case 内实现 tmux session 检测 + 自我重 exec）

**关键接口**：
```bash
gitea-watcher dashboard
# pseudo:
#   if [ -z "$TMUX" ] && ! tmux has-session -t agent-court-dashboard; then
#     tmux new-session -d -s agent-court-dashboard -n watcher \
#       "exec $0 dashboard --inside-tmux"
#     exec tmux attach -t agent-court-dashboard
#   fi
#   exec python -m gitea_watcher loop --mode dashboard
```

**依赖**：T-13-03 / T-13-04
**验收**：手动跑 `bin/gitea-watcher dashboard` → `tmux ls` 看到 `agent-court-dashboard: 1 windows`；再跑一次不重复建。

---

### 4.3 M3 · 双通道通知 + 仲裁锁（3 任务，1.5 人日）

#### T-13-07 · `dual_channel_approval.py` 核心 — 中

**目标**：实现 pending-approval 文件总线 + fcntl.flock 锁 + 阻塞等审批。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/dual_channel_approval.py`（新）

**关键数据结构**：
```
$COURT_ROOT/gitea-watcher/pending-approval/
  <repo-slug>-<num>.json     # {stage, repo, number, payload, created_at, channels[]}
  <repo-slug>-<num>.lock     # fcntl 排它
  <repo-slug>-<num>.result   # {verdict: approve|reject|edit, winner, reason, at}
```

**关键接口**：
```python
@dataclass(frozen=True)
class Verdict:
    approved: bool
    winner: str            # "terminal" | "wechat" | "feishu"
    reason: str            # reject/edit 时填
    edit_instruction: str  # verdict=edit 时填, 注入到 Claude window

def request_intake(repo, num, issue_detail, shenli_decision) -> Verdict:
    """① 写 pending-approval/<id>.json (stage=INTAKE)
       ② 调 notify_terminal + notify_im
       ③ 阻塞 _wait_for_result(<id>, timeout=24h)
       ④ 读 .result, 返回 Verdict, 清理 lock/json"""

def request_plan(repo, num, plan_text, window_name) -> Verdict:
    """同上, stage=PLAN; plan_text 嵌进 IM 消息体"""

def submit_verdict(repo, num, *, verdict, winner, reason="", edit="") -> bool:
    """终端 stdin / IM reply 调用. 用 flock 抢锁, 写 .result, 失败返 False(已被抢)."""
```

**仲裁锁实现**：
```python
def submit_verdict(...):
    lock_path = pending_dir / f"{slug}-{num}.lock"
    with open(lock_path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False   # 已有 winner
        result_path = pending_dir / f"{slug}-{num}.result"
        if result_path.exists():
            return False   # 已落地
        result_path.write_text(json.dumps({...}, ensure_ascii=False))
        return True
```

**依赖**：无
**验收**：`pytest tests/test_dual_channel_approval.py -v`：用线程并发 100 次 submit_verdict，验证仅 1 个返 True，文件内容来自 winner。

---

#### T-13-08 · 终端通道接入（stdin REPL） — 小

**目标**：watcher 在 dashboard 模式跑 `request_intake` 时，stdout 打印审批面板 + 同步 select.select() 监听 stdin，用户敲 `可以` / `改 X` / `拒` 立即调 `submit_verdict(winner="terminal")`。

**产出文件**：
- `dual_channel_approval.py` 内 `_wait_for_result()` 改造为同时监听 stdin + 文件 mtime

**关键接口**：
```python
def _wait_for_result(slug_id: str, timeout: int = 86400) -> dict:
    """select 监听:
       - sys.stdin (终端输入)
       - poll <result>.json 存在性 (IM 通道落地)
       任一就绪 → 解析 → 返回"""
```

**stdin 协议**（简单）：
```
[approval] K2Lab/moras-finder#42  stage=INTAKE
   title: ETL 报错修复
   shenli: GO (project=issue-42-etl-fix)
   reply 可以 / 改 <new_instruction> / 拒 <reason> ?
> 可以
[ok] terminal verdict=approve recorded
```

**依赖**：T-13-07
**验收**：`echo "可以" | python -c "from dual_channel_approval import request_intake; ..."` 自动 verdict=approve。

---

#### T-13-09 · IM 通道复用 PR-5/PR-6 接入 — 中

**目标**：调用现有 `shenpi.py` + `shenpi_channels/` 把审批面板推送到微信/飞书；IM reply 通过现有 `bin/court-approve` 或 MCP `pizhun` 入站 → 落地到 `<repo-num>.result`。

**产出文件**：
- `dual_channel_approval.py` 加 `_notify_im(channels, body)` 与 `_install_pizhun_bridge()`
- 文档（M-DOC-01）写如何配 channels.yaml

**关键接口**：
```python
def _notify_im(channels: list[str], body: str, project: str = "gitea-watcher") -> None:
    """直接调 shenpi_channels 模块的 send().
       project="gitea-watcher" 是 PR-5 体系下的伪 project,
       court_root/projects/gitea-watcher/bus/<peer>/pending-approval/ 对齐."""

# pizhun 桥: 当 cc-connect 调 mcp pizhun(project="gitea-watcher", id, approve)
#   → server.py 已挂的 mcp 工具会调 shenpi.approve()
#   → shenpi.approve 走 bus 文件移动
#   ★ 我们 hook 一层: 把 bus 文件移动事件桥接到 dual_channel_approval 的 .result 写入
```

**复用 PR-5/PR-6 的具体接口**：
| 调用方 | PR-5/6 接口 | 用途 |
|---|---|---|
| `_notify_im` | `shenpi_channels.feishu.send(title, body)` / `shenpi_channels.wechat.send(...)` | 推送审批面板 |
| pizhun 桥 | `shenpi.list_pending(project="gitea-watcher")` / `shenpi.approve(project, id)` | IM reply 入站 |
| 凭证 | `gitea_credentials.py` 的 keychain 读法 | 沿用，不新增 |

**依赖**：T-13-07
**验收**：mock shenpi_channels.feishu.send → 验证 `request_intake` 触发 1 次调用，body 含 issue 链接 + plan 摘要；mock `shenpi.approve` 触发 → `<repo-num>.result` 在 200ms 内出现。

---

### 4.4 M4 · issue-resolver skill（3 任务，1.5 人日）

#### T-13-10 · skill 主体（流程定义） — 中

**目标**：写 issue-resolver SKILL.md，定义 Claude window 内自跑的完整流程（审需求 → shenli → 出 plan → 暂停等审批 → 实施 → commit/push → 评论）。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/.claude/skills/issue-resolver/SKILL.md`（新，参考 `.claude/skills/shenli/SKILL.md` 格式）

**frontmatter**：
```yaml
---
name: issue-resolver
description: 处理单个 Gitea issue 的完整闭环 skill, 由 dashboard 模式 spawn 的独立 Claude CLI 使用.
trigger: stdin 收到 "ISSUE_RESOLVER_BEGIN <repo> <num>" 标记
---
```

**流程章节**（skill 正文）：
1. **审需求**：读 stdin 注入的 issue JSON（title/body/comments）
2. **shenli**：调本地 `python -m shenli --input <临时 md>`，确认仍然 GO
3. **出 plan**：写到 `$COURT_ROOT/gitea-watcher/pending-approval/<repo>-<num>-plan.md`
4. **暂停等审批**：调 `python -m dual_channel_approval request-plan <repo> <num>`，阻塞读返回的 Verdict
5. **实施**：在 issue 关联仓库下，按 plan 改代码（branch=`auto/issue-<num>/<short-slug>`）
6. **commit + push**：commit trailer 必含 `Issue: <repo>#<num>`，push 前 pre-push hook 自动验
7. **评论 issue**：调 MCP `comment_on_issue(repo, num, summary)`

**依赖**：M3 完成
**验收**：手动 cat SKILL.md → 7 步骤齐全；frontmatter `yq` 解析通过。

---

#### T-13-11 · `issue_resolver.py` 辅助模块 — 中

**目标**：把 issue 数据通过 stdin 注入到 Claude CLI 启动；封装 `request-plan` CLI（供 SKILL.md 内调用）。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/issue_resolver.py`（新）

**关键接口**：
```python
def build_intro_message(issue_detail, comments, decision) -> str:
    """组装注入 Claude window 的初始消息:
       ISSUE_RESOLVER_BEGIN <repo> <num>
       --- issue.yaml ---
       <frontmatter>
       --- body ---
       ...
       --- shenli.decision ---
       <json>"""

# CLI 入口 (skill 内调):
#   python -m issue_resolver request-plan --repo X --num N --plan-file P
#   python -m issue_resolver report-back --repo X --num N --summary-file S
```

**依赖**：T-13-07
**验收**：`python -m issue_resolver request-plan --help` 输出参数；`build_intro_message` 单测覆盖空 comments / 长 body 截断。

---

#### T-13-12 · `bin/spawn-issue-window` — 小

**目标**：封装 tmux new-window + claude CLI 启动 + stdin 注入，被 watcher 调用。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/bin/spawn-issue-window`（新，bash）

**关键用法**：
```bash
spawn-issue-window <repo> <num> <intro-file>
# 内部:
#   WIN=$(python -c "from dashboard_tmux import issue_window_name; print(issue_window_name('$1','$2'))")
#   SKILL=$(cat .claude/skills/issue-resolver/SKILL.md)
#   tmux new-window -t agent-court-dashboard -n "$WIN" \
#     "cat $3 | claude --append-system-prompt \"$SKILL\""
```

**风格参考**：`bin/role-launch:52`（`--append-system-prompt "$(cat ...)"` 模式）

**依赖**：T-13-04 / T-13-11
**验收**：手动 mock 一个 intro 文件 → `bin/spawn-issue-window K2Lab/test 99 /tmp/intro` → `tmux list-windows -t agent-court-dashboard` 看到新 window；window 内 `claude` 进程在跑。

---

### 4.5 M5 · 状态机扩展（2 任务，0.5 人日）

#### T-13-13 · seen-issues.json schema 扩展 — 小

**目标**：扩 `_build_seen_entry`，新增 dashboard 专属字段；提供 migration 兼容旧 json。

**产出文件**：
- `gitea_watcher.py` 改 `_build_seen_entry`

**新增 last_action 取值**：
| 取值 | 触发时机 |
|---|---|
| `AWAITING_INTAKE_APPROVAL` | request_intake 阻塞期间（中间态，落盘只在崩溃恢复时见） |
| `AWAITING_PLAN_APPROVAL` | request_plan 阻塞期间 |
| `DISPATCHED_DASHBOARD` | intake approve 后，window 已 spawn |
| `EXECUTING` | plan approve 后（由 issue_resolver.report-back 写入） |
| `DONE_DASHBOARD` | commit/push/comment 三件套完成 |
| `REJECTED_DASHBOARD` | 任一阶段 reject |

**新增字段**：
```json
{
  "approval_winner": "terminal|wechat|feishu",
  "dispatched_at": "ISO8601",
  "tmux_window": "K2Lab-moras-finder-42",
  "stage": "INTAKE|PLAN|EXECUTING|DONE"
}
```

**兼容性**：旧 entry 无新字段 → 默认 None，读取代码用 `.get()`。

**依赖**：T-13-02
**验收**：`pytest tests/test_seen_state_schema.py`：加载 PR-12 时代的 seen-issues.json fixture，不抛异常；新写入再读取，新字段保持。

---

#### T-13-14 · 状态查询 CLI — 小

**目标**：给运维一个看当前 dashboard 状态的 CLI。

**产出文件**：
- `gitea_watcher.py` 新增 `status` 子命令

**用法**：
```
python -m gitea_watcher status --mode dashboard
# 输出表格:
# repo                num  stage     winner    tmux_window               since
# K2Lab/moras-finder  42   EXECUTING terminal  K2Lab-moras-finder-42     12m
```

**依赖**：T-13-13
**验收**：`python -m gitea_watcher status --mode dashboard` 退出码 0，且至少打印表头。

---

### 4.6 M6 · IM reply → tmux 注入桥（2 任务，1 人日）

#### T-13-15 · `im_reply_router.py` 监听器 — 中

**目标**：起一个后台线程（watcher loop 内 spawn），监听 `pending-approval/*.result` 落地，根据 stage 注入 tmux 或触发 spawn。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/im_reply_router.py`（新）

**关键接口**：
```python
class ImReplyRouter:
    def __init__(self, court_root, tmux=dashboard_tmux): ...

    def start(self) -> None:
        """后台 thread, 用 polling (interval=1s) 而非 fswatch,
           原因: 跨平台 + 0 依赖 + result 文件量小."""

    def _handle_result(self, result_path: Path) -> None:
        meta = json.loads(result_path.read_text())
        stage = meta["stage"]
        verdict = meta["verdict"]
        if stage == "INTAKE":
            if verdict == "approve":
                spawn_issue_window(...)
            else:
                comment_on_issue(reject_reason)
        elif stage == "PLAN":
            window = meta["tmux_window"]
            if verdict == "approve":
                tmux.inject(window, "可以", send_enter=True)
            elif verdict == "edit":
                tmux.inject(window, f"改 {meta['edit_instruction']}", send_enter=True)
            else:
                tmux.inject(window, f"拒 {meta['reason']}", send_enter=True)
```

**依赖**：T-13-04 / T-13-07 / T-13-12
**验收**：`pytest tests/test_im_reply_router.py`：mock 写 result 文件 → 验证 INTAKE+approve 调 spawn_issue_window；PLAN+edit 调 tmux.inject("改 X")。

---

#### T-13-16 · 与 watcher loop 集成 — 小

**目标**：`gitea_watcher.loop()` mode=dashboard 时启动 ImReplyRouter；优雅退出时停。

**产出文件**：
- `gitea_watcher.py`（修改 `loop()`）

**关键改动**：
```python
def loop(self):
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
        if router: router.stop()
```

**依赖**：T-13-15
**验收**：`bin/gitea-watcher dashboard` 跑起来后，`ps -ef | grep im_reply_router` 看到后台 thread（同进程，无独立 ps，验 `py-spy dump` 或加 log "router started"）。

---

### 4.7 集成测试（2 任务，0.5 人日）

#### T-13-INT-01 · 端到端手工脚本 — 中

**目标**：从 mock 一个 issue → 启 watcher dashboard → 终端 approve intake → 验 tmux window 出现 → 终端 approve plan → 验 commit/push 走 hook → 验 issue 评论。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/tests/dashboard/e2e_terminal.sh`（新）

**关键内容**：
- 用一个本地 gitea container（或 mock GiteaClient）造 issue#999
- 注入 stdin 序列 `可以\n` 两次
- 验收：脚本退出码 0；`tmux list-windows -t agent-court-dashboard | grep 999`；`git log --grep="Issue: .*#999"` 命中

**依赖**：M1-M6 全部
**验收**：脚本一次跑过。

---

#### T-13-INT-02 · 双通道仲裁竞态测试 — 中

**目标**：模拟终端 + IM 同毫秒 reply 同一 issue，验最终只有 1 个 winner，另一通道收到「已被 winner 处理」反馈。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/tests/dashboard/test_race.py`（pytest，新）

**关键内容**：
```python
def test_intake_race_terminal_wins():
    # 1. request_intake 在背景线程跑
    # 2. 同时调 submit_verdict("terminal", approve) 和 submit_verdict("feishu", approve)
    # 3. 用 threading.Barrier 确保起跑线一致
    # 4. 断言: 一个 True 一个 False; .result 内 winner 字段确定
```

**依赖**：T-13-07
**验收**：100 次循环，无 race 漏判。

---

### 4.8 文档（2 任务，0.5 人日）

#### T-13-DOC-01 · `docs/dashboard-mode.md` — 中

**目标**：写用户向导：启动 / IM 通道配置 / 审批面板示例 / 常见排障。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/docs/dashboard-mode.md`（新）

**目录**：
1. 什么是 dashboard 模式（vs court 模式）
2. 启动：`bin/gitea-watcher dashboard`
3. 双通道配置：终端默认开 / IM 走 `channels.yaml`（指向 PR-5 文档）
4. 审批面板范例（终端 + 微信截图占位）
5. 排障：tmux 找不到 / Claude CLI 卡死如何 kill / pending-approval 积压清理
6. 与 court 模式切换：`WATCHER_MODE` env / launchd 重 install

**依赖**：M1-M6 完成
**验收**：md 文件 mdformat 通过；目录 6 节齐全。

---

#### T-13-DOC-02 · README 加 dashboard 段 — 小

**目标**：README 顶部加「两种模式对比表」。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/README.md`（修改）

**改动**：
```markdown
## 两种运行模式

| 模式 | 触发命令 | 行为 | 适用场景 |
|---|---|---|---|
| court (默认) | `gitea-watcher start` (launchd) | 黑盒, watcher 自动 court-up 多 role | 信任度高 / 量大 / 不需观察 |
| dashboard | `gitea-watcher dashboard` (前台 tmux) | 双通道审批 + 独立 Claude window + 二次审批 | 谨慎试运行 / 远程 IM 控制 |
```

**依赖**：T-13-DOC-01
**验收**：README diff 仅 1 节，无 scope creep。

---

### 4.9 收尾（2 任务，0.5 人日）

#### T-13-99-A · pyproject.toml 注册新 py-modules — 小

**目标**：把 `dashboard_tmux` / `dual_channel_approval` / `issue_resolver` / `im_reply_router` 加进 py-modules，否则 `python -m xxx` 找不到。

**产出文件**：
- `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/pyproject.toml`

**验收**：`cd mcp/court-mcp && uv pip install -e . && python -c "import dashboard_tmux, dual_channel_approval, issue_resolver, im_reply_router"` 0 报错。

---

#### T-13-99-B · 提交前自检 + commit 拆分预演 — 小

**目标**：跑 §K2Work 提交规范的全部 checklist；按 PR 拆分建议（见 §7）出 commit 序列预演。

**验收**：
- `git status --short | grep -E '\.claude/|\.context/|CLAUDE\.md'` 空
- `bash tests/test_pre_push_hook.sh` 4 场景全过
- `git log --oneline feat/pr-12-issue-driven..HEAD` commit 数符合 §7 拆分

---

## 5. 依赖图

```
                                                      [关键路径用 ★ 标]

  T-13-01 (CLI 解析) ──────────┐
       │                       │
       ▼                       ▼
  T-13-03 (bin 子命令)     T-13-04 ★ (tmux 模块) ──── T-13-05 (escape)
                               │                            │
                               ▼                            │
                          T-13-06 (启动入口)                 │
                                                            ▼
                          T-13-07 ★ (dual_channel) ── T-13-12 ★ (spawn-window)
                               │                            ▲
                       ┌───────┼───────┐                    │
                       ▼       ▼       ▼                    │
                 T-13-08    T-13-09  T-13-11 (issue_resolver helper)
                 (terminal) (IM)        │
                                        ▼
                                   T-13-10 (skill md)
                                        │
                       ┌────────────────┼──────────────┐
                       ▼                ▼              ▼
                  T-13-02 ★          T-13-15 ★      T-13-13
                  (run_once 分流)    (reply router)  (state schema)
                       │                │              │
                       └──────┬─────────┘              ▼
                              ▼                    T-13-14 (status CLI)
                         T-13-16 (loop 集成)
                              │
              ┌───────────────┼───────────────────┐
              ▼               ▼                   ▼
         T-13-INT-01      T-13-INT-02        T-13-DOC-01
         (e2e)            (race)             (用户文档)
              │                                   │
              └──────────────┬────────────────────┘
                             ▼
                        T-13-DOC-02 (README)
                             │
                             ▼
                        T-13-99-A / 99-B (收尾)
```

**关键路径**（约 4.5 人日）：T-13-04 → T-13-07 → T-13-12 → T-13-15 → T-13-16 → T-13-INT-01

**可并行**：M1 与 M2 / M3 与 M4-skill / DOC 与 INT-02

---

## 6. PR 拆分建议

| 方案 | 推荐度 | 说明 |
|---|---|---|
| **方案 A：一次性 PR-13** | ★★ | 6 模块紧耦合，review 难度高（~1500 行）；但状态一致性好 |
| **方案 B：PR-13a + PR-13b** | ★★★★★ | 推荐 |
| **方案 C：6 个独立 PR** | ★ | 链路长，每个 stacked，rebase 风险高 |

### 方案 B 拆分细则

| PR | 包含任务 | 行数预估 | 目的 |
|---|---|---|---|
| **PR-13a · 基础设施** | M2 (T-04~06) + M3 (T-07~09) + M5 (T-13~14) | ~700 行 | 无副作用底座，单测可全覆盖；court 模式行为零变化 |
| **PR-13b · 端到端串联** | M1 (T-01~03) + M4 (T-10~12) + M6 (T-15~16) + INT + DOC + 收尾 | ~900 行 | 把 PR-13a 的部件串成完整 dashboard 闭环 |

**优势**：
1. PR-13a 即使先合也不破坏 PR-12（mode 默认 court，新模块未挂接）
2. PR-13b 集中 review 流程编排，更聚焦
3. 万一 PR-13b 发现设计问题，PR-13a 仍可独立保留

---

## 7. 风险清单

| # | 风险 | 影响 | 缓解措施 |
|---|---|---|---|
| R1 | **tmux send-keys 注入被 Claude CLI 的 prompt 解析吞掉**（用户正在输入时被打断） | 高 · 审批信号丢失 | T-13-05 使用 `tmux send-keys -l` literal 模式；注入前 `tmux send-keys C-c` 中断当前输入；注入完单独 `send-keys Enter`；T-13-INT-01 专门测注入时机 |
| R2 | **IM webhook 延迟导致 race 判定误差** | 中 · 偶发 winner 错 | T-13-07 用 fcntl.LOCK_NB 原子抢锁 + 写 .result 二次校验；DR-5 终端 race 优先；T-13-INT-02 跑 100 次循环验 |
| R3 | **同 issue 反复触发**（intake 通过后用户编辑 issue → updated_at 变 → watcher 又抓 → 重复 spawn） | 高 · 多 window 互相覆盖 | T-13-13 stage 字段 + T-13-02 检查：last_action ∈ {DISPATCHED_DASHBOARD, EXECUTING} 时跳过新 spawn，只走 `tmux inject` 通知现有 window「issue 已更新」 |
| R4 | **Claude CLI 卡死 / OOM** | 中 · window 僵尸 | T-13-14 status CLI 暴露 `tmux_window` + 启动时间；提供 `bin/gitea-watcher kill-window <repo> <num>` 子命令；24h 超时 auto-reject 释放 pending-approval |
| R5 | **tmux session 被用户手动 kill 后 watcher 状态错乱** | 高 · 状态全乱 | T-13-04 `ensure_session` 幂等；T-13-15 router 每次 inject 前 `window_exists()` 检查，失败则降级为 `report_back("window 丢失，issue 回退 PENDING_RETRY")` |
| R6 | **IM 通道凭证失效** | 中 · 单通道挂 | 沿用 PR-5 `record_error` + osascript 通知；T-13-07 `_notify_im` 单通道失败不阻塞主流程，终端仍可 approve |
| R7 | **pending-approval 文件积压不清理** | 低 · 磁盘+IO | 沿用 PR-5 `sweep_expired`（24h auto-deny）；T-13-DOC-01 排障章节附 `find pending-approval -mtime +1 -delete` |
| R8 | **PR-12 court 模式被 dashboard 模式不小心污染** | 极高 · 回归 | T-13-01 mode 强校验 + T-13-02 dashboard 路径完全新写不改 `_apply_decision`；T-13-INT-01 用 PR-12 的现成 e2e 跑一遍验证 court 模式回归 0 |
| R9 | **commit/push 阶段 pre-push hook 拒（分支前缀/trailer 缺失）→ Claude window 不知如何处理** | 中 · 阻塞 | T-13-10 skill 内显式步骤「捕获 push 失败 → 读 hook stderr → 重命名分支 / 补 trailer 重试」；hook 拒绝输出格式 PR-12 已固定不改 |
| R10 | **stdin REPL 与 watcher loop 抢 stdin**（终端跑 watcher 时 stdin 既是审批输入又被 loop 用） | 中 · 输入串流 | T-13-08 用 `select.select([sys.stdin], [], [], 0.5)` 非阻塞读 + 输入行加 prefix `[approval] reply >` 提示；loop 主线程不读 stdin |

---

## 8. 验收清单（10 条对齐需求）

| # | 验收标准 | 对应任务 |
|---|---|---|
| 1 | `bin/gitea-watcher` 默认行为不变（court 模式回归 PR-12 测试 0 失败） | T-13-01, T-13-02, T-13-INT-01 |
| 2 | `bin/gitea-watcher dashboard` 进入前台 tmux session `agent-court-dashboard` | T-13-03, T-13-06 |
| 3 | 抓到新 issue 后立刻收到双通道通知（终端 + 至少 1 个 IM） | T-13-07, T-13-08, T-13-09 |
| 4 | 终端 / IM 任一通道 approve intake → 自动 spawn tmux window 跑 Claude | T-13-12, T-13-15 |
| 5 | Claude window 内 shenli → 出 plan → 触发二次审批，watcher 收到 plan 通知 | T-13-10, T-13-11 |
| 6 | 任一通道 approve plan → tmux inject "可以" → Claude 继续实施 | T-13-15 |
| 7 | commit/push 完整走 PR-12 pre-push hook（分支前缀 + trailer 检查通过） | T-13-10, T-13-99-B |
| 8 | issue 自动评论（含 winner / commit hash） | T-13-11 |
| 9 | 同毫秒双通道 reply 仅 1 个 winner，状态机一致 | T-13-07, T-13-INT-02 |
| 10 | 文档齐全（docs/dashboard-mode.md + README 段落） | T-13-DOC-01, T-13-DOC-02 |

---

## 9. 本次不做（明确范围外）

- ❌ Web UI / 浏览器看板（坚持 tmux）
- ❌ 多 Gitea 实例 / 多 token 并行
- ❌ Claude window 内的 token / cost 统计可视化
- ❌ 重写 PR-5 的 shenpi 动词体系（pizhun/pibo 保留）
- ❌ 替换 polling 为 fswatch（M6 暂用 1s polling，10 issue/min 量级足够）
- ❌ 跨机器分布式 dashboard（单机 tmux）
- ❌ PR-12 court 模式的任何行为修改

---

## 10. PR-12 / PR-5/6 兼容性矩阵

### 10.1 PR-12 不能破坏的行为

| 行为 | 当前实现位置 | 本次如何保证 |
|---|---|---|
| `gitea-watcher start / stop / status / logs / --once` 子命令 | `bin/gitea-watcher:22-57` | T-13-03 只加 `dashboard` case，不动现有 |
| `_apply_decision` GO/NEED_INFO/REJECT/PENDING_RETRY 分支 | `gitea_watcher.py:201-257` | T-13-02 新写 `_apply_decision_dashboard`，不动原方法 |
| `seen-issues.json` 旧字段（repo/number/updated_at/last_action/court_project/shenli_run_at/retry_at） | `gitea_watcher.py:117-127` | T-13-13 只加字段不改名 |
| `migrate-to-court` + `court-up` 流程 | `gitea_watcher.py:230-237` | dashboard 模式不调用，court 模式不变 |
| pre-push hook（分支前缀 / 禁 force / 禁 main / commit trailer） | `bin/migrate-to-court` 注入 | T-13-10 复用，不改 hook 文件本身 |
| `tests/test_pre_push_hook.sh` 4 场景 | `tests/test_pre_push_hook.sh` | T-13-99-B 必须全过 |

### 10.2 PR-5 / PR-6 必须复用且不能改的接口

| 接口 | 文件 / 函数 | 调用方 |
|---|---|---|
| `shenpi_channels.feishu.send` / `.wechat.send` | `mcp/court-mcp/shenpi_channels/*` | T-13-09 `_notify_im` |
| `shenpi.approve(project, msg_id)` / `shenpi.deny` | `mcp/court-mcp/shenpi.py` | pizhun 桥（IM reply 入站时被 MCP 调） |
| `bin/court-approve <project> approve <id>` | `bin/court-approve` | 终端 IM 模拟 reply 用 |
| `pizhun` MCP tool | `mcp/court-mcp/server.py` 已挂 | cc-connect → IM → pizhun → bus 文件移动 |
| `bus/<peer>/pending-approval/` 文件总线 | PR-5 约定 | T-13-09 桥接：把 bus 文件移动事件 → dual_channel_approval `.result` |

### 10.3 新增 vs 复用比例

| 类别 | 新文件 | 修改现有 |
|---|---|---|
| Python 模块 | 4（dashboard_tmux / dual_channel_approval / issue_resolver / im_reply_router） | 1（gitea_watcher.py） |
| Bash 脚本 | 1（spawn-issue-window） | 1（gitea-watcher） |
| 配置 | 0 | 2（pyproject.toml / plist 模板） |
| 文档 | 1（dashboard-mode.md） | 1（README.md） |
| Skill | 1（issue-resolver/SKILL.md） | 0 |
| 测试 | 3（e2e_terminal.sh / test_race.py / test_seen_state_schema.py） | 0 |

**新增占比 ≈ 70%**，符合「夹层 PR」预期（新模式 80% 是新代码 + 20% 接口缝合）。

---

## 11. 工时分配总览

| 模块 | 任务数 | 工时（人日） | 关键路径 |
|---|---|---|---|
| M1 watcher 双模式 | 3 | 1.5 | T-13-02 |
| M2 控制塔 tmux | 3 | 1.5 | T-13-04 |
| M3 双通道审批 | 3 | 1.5 | T-13-07 |
| M4 issue-resolver skill | 3 | 1.5 | T-13-12 |
| M5 状态机扩展 | 2 | 0.5 | — |
| M6 reply router | 2 | 1.0 | T-13-15 |
| 集成测试 | 2 | 0.5 | T-13-INT-01 |
| 文档 | 2 | 0.5 | — |
| 收尾 | 2 | 0.5 | T-13-99-B |
| **合计** | **22** | **9.0** | |

> 工时含联调踩坑 buffer。串行最小路径 ~4.5 人日；并行 2 人开发可压到 5 人日。

---

## 12. 实施前必做的 5 件事

1. **确认 PR-12 已 merge 或至少 lock-in**：本 PR 基于 `feat/pr-12-issue-driven` stacked，PR-12 若 force-push 本分支需 rebase
2. **本地起 channels.yaml**（指向 PR-5 文档），验证 `shenpi_channels.feishu.send("test", "ping")` 成功推送
3. **本地 tmux 3.6a 验证**：`tmux new-session -d -s test; tmux send-keys -t test -l "可以"; tmux send-keys -t test Enter; tmux capture-pane -t test -p` 看到「可以」被字面送达
4. **跑 PR-12 e2e 基线**：`bash tests/test_pre_push_hook.sh && python -m gitea_watcher run-once`，记录通过状态作为回归基线
5. **跟用户确认 IM 通道首推顺序**：本规划默认 `terminal + feishu + wechat` 同时推；用户若想 staged（先终端 → 30s 没回再推 IM）需加 T-13-EXTRA 任务，暂不在范围内

---

**END · PR-13 dashboard-mode.md**
