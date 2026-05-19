# 功能规划：PR-12 工单驱动模式 (issue-driven court)

| 字段 | 值 |
|---|---|
| **PR 号** | PR-12（独立新分支，不动 PR-7 / PR-8~11 既定路线） |
| **规划日期** | 2026-05-19 |
| **规划承接** | PR-7 已 push（onboard 联邦），PR-8~11 路线图（监控窗 / 二段审核 / 双层记忆）已归档；本 PR 落地"工单粒度"上游入口 |
| **总任务数** | 27（M1×5 + M2×4 + M3×6 + M4×6 + M5×4 + INT×2） |
| **总工时估算** | **6.5 ~ 8 人日**（中等估算 7 人日） |

---

## 1. 一句话目标

把 agent-court 上游派活儿入口从 "本地手工敲 court-send" 升级为 **"git.k2lab.ai 上有 issue 指派给我 → 守护进程自动拉取 → shenli skill 审理 → GO 即 spawn 新 court project + agent team 全链路跑完 → 在 issue 评论汇报结果"**，全程仅在 NEED_INFO / REJECT 时人工介入。

---

## 2. 架构图（数据流）

```
                       git.k2lab.ai (Gitea v1)
                              │
                              │  GET /api/v1/repos/issues/search?assigned=true
                              │  (PAT from macOS Keychain via `git credential-osxkeychain get`)
                              ▼
              ┌───────────────────────────────────┐
              │  gitea-watcher daemon (launchd)   │
              │  • 30s 轮询                       │
              │  • 状态文件 seen-issues.json      │
              │  • diff 新/更新 issue             │
              └──────────────┬────────────────────┘
                             │  写 pending-shenli/<repo>-<num>.md
                             ▼
              ┌───────────────────────────────────┐
              │  shenli skill (审理决策)          │
              │  GO / NEED_INFO / REJECT          │
              │  输出 court.yaml roles 计划       │
              └──┬──────────────┬─────────────────┘
        NEED_INFO│              │GO            │REJECT
                 │              │              │
                 ▼              ▼              ▼
     comment_on_issue   court-up <project>  comment + close
     (@发起人追问)       + dispatch_to_foreman  (写原因)
                              │
                              ▼
              ┌───────────────────────────────────┐
              │  court project (一 issue 一座)    │
              │  ~/.agent-court/projects/         │
              │    issue-<repo_slug>-<num>/       │
              │  tmux session: court-<repo>-<n>   │
              │  roles: foreman / dev / qa / ...  │
              └──────────────┬────────────────────┘
                             │  调研 → 开发 → review → commit → push
                             ▼
              ┌───────────────────────────────────┐
              │  foreman 回执 → upstream inbox    │
              │  watcher 读到 → comment_on_issue  │
              │  (附 PR 链接 / 结果摘要)          │
              └───────────────────────────────────┘
```

---

## 3. 关键设计决策记录

| # | 决策 | 理由（一句话） |
|---|---|---|
| D1 | **凭证从 macOS Keychain 取，不落盘** | 用户 Keychain 已有 `oauth2` token 且验证可用，落 `.env` 会增加泄漏面 |
| D2 | **fallback 顺序：keychain → `K2LAB_GIT_TOKEN` env → `~/.netrc`** | launchd 子进程默认无 GUI keychain 访问权，env 兜底；netrc 兜底给纯 CLI 环境 |
| D3 | **轮询 30s 而非 webhook** | Gitea webhook 需公网回调 + 反向代理，开发机不具备；30s 延迟可接受 |
| D4 | **一 issue 一 court project** | 项目级隔离，避免 issue A 的 agent team 串扰 issue B；court 本身轻量（目录 + tmux session） |
| D5 | **court 命名 `issue-<repo_slug>-<num>`，repo_slug=`owner-repo`** | 全局唯一 + 可读 + 便于反查 issue（如 `issue-k2lab-moras-finder-123`） |
| D6 | **shenli 是 skill 而非 daemon 内嵌函数** | 决策逻辑需复用 LLM 推理能力，skill 走 Claude Code CLI 自然衔接；daemon 只做"喂数据 + 跑 skill + 拿 JSON" |
| D7 | **全自动模式，仅 NEED_INFO/REJECT 停下** | 用户明确要求；GO 链路若出问题，PR-8 二段复核会兜底（交叉引用） |
| D8 | **seen-issues.json 不进 git** | 含本机时间戳 + token 取回的元数据，且不同机器轮询节奏不同 |
| D9 | **token 失效时退出而非重试** | 防止 401 风暴 + 触发 Gitea rate limit ban；通过 macOS notification 让用户感知 |
| D10 | **gitea-watcher 与 court-watcher 分离** | 职责单一：gitea-watcher 管"外部 issue → court project 生成"；court-watcher 管"court 内部 bus 路由" |

### 3.1 审理决策表（shenli skill 用）

| 决策 | 触发条件 | 后续动作 |
|---|---|---|
| **GO** | 描述完整 + 有验收标准（"如何验证" / "完成定义"） + 范围明确 | spawn court project + dispatch_to_foreman |
| **NEED_INFO** | 描述缺少：技术细节 / 验收标准 / 所在仓库 / 复现步骤 | comment_on_issue 列出缺失项 + @发起人 |
| **REJECT** | 重复 issue（关键字命中已 close 的） / 超出 agent-court 能力范围（如纯硬件问题） / 标签含 `wontfix` `duplicate` `out-of-scope` | comment 写原因 + transition_issue(state=closed) |

---

## 4. WBS 任务表

### M1 · Gitea 客户端 + 凭证适配器（1.5 人日）

#### T-12-01 · KeychainCredentialProvider（凭证适配器）

- **目标**：把"从 macOS Keychain 取 Gitea PAT"封装为一个无状态可测试的 Python 类
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_credentials.py`
- **关键接口**：
  ```python
  class KeychainCredentialProvider:
      def __init__(self, host: str = "git.k2lab.ai"): ...
      def get_token(self) -> str:  # 抛 CredentialNotFoundError
          """fallback: keychain -> $K2LAB_GIT_TOKEN -> ~/.netrc"""
      def get_username(self) -> str:  # 默认 "oauth2"
  ```
- **依赖**：复用现有 `subprocess` 调 `git credential-osxkeychain get`；无新增第三方包
- **复用**：参考 `mcp/court-mcp/server.py` 第 26~38 行 import 风格
- **验收命令**：
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp && \
    python -c "from gitea_credentials import KeychainCredentialProvider; print(KeychainCredentialProvider().get_token()[:8] + '...')"
  ```
- **估算**：小（~1.5h）

#### T-12-02 · GiteaClient 核心（list_assigned_issues / get_issue）

- **目标**：实现读路径（轮询要用的）
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_client.py`
- **关键接口**：
  ```python
  class GiteaClient:
      def __init__(self, base_url="https://git.k2lab.ai/api/v1",
                   provider: KeychainCredentialProvider = None,
                   timeout: float = 10.0): ...
      def whoami(self) -> dict:  # GET /user
      def list_assigned_issues(self, state: str = "open",
                                since: Optional[str] = None) -> list[dict]:
          # GET /repos/issues/search?assigned=true&state=...&since=ISO8601
      def get_issue(self, repo: str, number: int) -> dict:
          # GET /repos/{repo}/issues/{number}  # repo = "owner/name"
  ```
- **依赖**：T-12-01；用 `aiohttp`（项目已依赖）或 `requests`（更简单，建议 `requests`，加进 `pyproject.toml`）
- **验收命令**：
  ```bash
  python -m court_mcp.gitea_client whoami | jq .login
  python -m court_mcp.gitea_client list_assigned_issues | jq 'length'
  ```
- **估算**：中（~3h）

#### T-12-03 · GiteaClient 写路径（comment / transition）

- **目标**：实现 shenli 决策落地用的写接口
- **产出文件**：同上（追加方法）
- **关键接口**：
  ```python
  def comment_on_issue(self, repo: str, number: int, body: str) -> dict:
      # POST /repos/{repo}/issues/{number}/comments
  def transition_issue(self, repo: str, number: int, state: str) -> dict:
      # PATCH /repos/{repo}/issues/{number}  body={"state": "closed"|"open"}
  ```
- **依赖**：T-12-02
- **验收命令**：
  ```bash
  # 在测试 issue 上写评论
  python -m court_mcp.gitea_client comment_on_issue --repo K2Lab/agent-court-test --num 1 --body "ping from gitea_client"
  ```
- **估算**：小（~1.5h）

#### T-12-04 · `python -m court_mcp.gitea_client` CLI 入口

- **目标**：让守护进程 / shenli / 调试都能 shell 调
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/__main__.py`（如果还没有的话，建议同时给 `gitea_client` 加 `if __name__ == "__main__":` 入口）
- **关键接口**（argparse）：
  ```
  python -m court_mcp.gitea_client whoami
  python -m court_mcp.gitea_client list_assigned_issues [--state open|all] [--since 2026-05-01T00:00:00Z]
  python -m court_mcp.gitea_client get_issue --repo owner/name --num 123
  python -m court_mcp.gitea_client comment --repo owner/name --num 123 --body "..."
  python -m court_mcp.gitea_client transition --repo owner/name --num 123 --state closed
  ```
- **依赖**：T-12-01~03
- **验收命令**：见 T-12-02、T-12-03
- **估算**：小（~1h）

#### T-12-05 · GiteaClient 单测

- **目标**：阻断回归，主要 mock HTTP 层（HTTP 不算 DB，按 §3 测试规范允许 mock）
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/tests/test_gitea_client.py`
- **覆盖**：5 个方法各 1 个 happy path + 401/404 错误分支
- **依赖**：T-12-01~03
- **验收命令**：`cd mcp/court-mcp && python -m pytest tests/test_gitea_client.py -v`
- **估算**：小（~1.5h）

---

### M2 · MCP 工具扩展（0.5 人日）

> 在现有 `mcp/court-mcp/server.py` 末尾追加 4 个 `@mcp.tool()`，让上游 LLM 也能直接调 Gitea（不只是守护进程）

#### T-12-06 · `list_assigned_issues` MCP 工具
- **目标**：暴露读路径给 Claude Code 调用
- **产出文件**：`/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/server.py`（追加，~30 行）
- **关键接口**：
  ```python
  @mcp.tool()
  def list_assigned_issues(state: str = "open", since: Optional[str] = None) -> dict:
      """返回 {issues: [...], count: N, fetched_at: ISO8601}"""
  ```
- **依赖**：T-12-02
- **验收命令**：MCP inspector 调一次返回 200
- **估算**：小（~0.5h）

#### T-12-07 · `get_issue` MCP 工具
- **产出**：同上文件，签名 `get_issue(repo: str, number: int) -> dict`
- **估算**：小（~0.3h）

#### T-12-08 · `comment_on_issue` MCP 工具
- **产出**：同上文件，签名 `comment_on_issue(repo: str, number: int, body: str) -> dict`
- **估算**：小（~0.3h）

#### T-12-09 · `transition_issue` MCP 工具
- **产出**：同上文件，签名 `transition_issue(repo: str, number: int, state: str) -> dict`，state 只允许 `"open"` / `"closed"`，其他返回 `error: invalid_state`
- **估算**：小（~0.3h）

> 4 个工具均按现有 `server.py` 错误处理风格（返回 `{"error": ..., "detail": ...}` 而非 raise）

---

### M3 · gitea-watcher 守护进程（2 人日）

#### T-12-10 · gitea_watcher.py 主循环骨架

- **目标**：实现 30s 轮询 + diff 逻辑 + 状态文件读写
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_watcher.py`
- **关键数据结构**（seen-issues.json）：
  ```json
  {
    "K2Lab/moras-finder#123": {
      "updated_at": "2026-05-19T10:00:00Z",
      "last_action": "GO",          // GO | NEED_INFO | REJECT | NEW
      "court_project": "issue-k2lab-moras-finder-123",
      "shenli_run_at": "2026-05-19T10:00:05Z"
    }
  }
  ```
- **状态文件位置**：`~/.agent-court/gitea-watcher/seen-issues.json`（COURT_ROOT 派生）
- **关键接口**：
  ```python
  class GiteaWatcher:
      def __init__(self, poll_interval: int = 30, court_root: Path = None): ...
      def run_once(self) -> dict:  # returns {new: N, updated: M, errors: K}
      def loop(self):  # while True: run_once(); sleep(poll_interval)
      def _diff(self, current: list[dict]) -> tuple[list, list]:  # (new, updated)
  ```
- **依赖**：T-12-02
- **验收命令**：
  ```bash
  python -m court_mcp.gitea_watcher run-once
  cat ~/.agent-court/gitea-watcher/seen-issues.json | jq .
  ```
- **估算**：中（~3h）

#### T-12-11 · pending-shenli 文件投递

- **目标**：每个新 / 更新 issue 写一个 markdown 文件给 shenli 消费
- **产出文件**：扩展 `gitea_watcher.py`
- **关键产物**（`~/.agent-court/gitea-watcher/pending-shenli/<repo_slug>-<num>.md`）：
  ```markdown
  ---
  repo: K2Lab/moras-finder
  number: 123
  title: ...
  author: alice
  state: open
  updated_at: 2026-05-19T10:00:00Z
  url: https://git.k2lab.ai/K2Lab/moras-finder/issues/123
  labels: [bug, p1]
  ---

  ## Body
  <issue 正文>

  ## Comments
  - alice @ 2026-05-19T09:00:00Z: ...
  - bob @ 2026-05-19T09:30:00Z: ...
  ```
- **依赖**：T-12-10
- **验收命令**：`ls ~/.agent-court/gitea-watcher/pending-shenli/` 出现至少 1 个文件
- **估算**：小（~1.5h）

#### T-12-12 · shenli 调用胶水（subprocess → JSON 解析）

- **目标**：守护进程把 pending-shenli/<x>.md 喂给 shenli skill，拿 JSON 决策，按 GO/NEED_INFO/REJECT 分支调后续
- **产出文件**：扩展 `gitea_watcher.py` 增加 `_dispatch_shenli(pending_file: Path) -> dict`
- **关键接口**：
  ```python
  # subprocess 调 claude code 跑 shenli skill
  # cmd = ["claude", "--skill", "shenli", "--input", str(pending_file)]
  # 解析最后一行 JSON
  ```
- **依赖**：T-12-11、T-12-14（shenli skill 必须先存在）
- **验收命令**：手动构造一个 pending md，跑 watcher run-once，看 seen-issues 状态变化
- **估算**：中（~2h，含异常分支）

#### T-12-13 · bin/gitea-watcher bash launcher

- **目标**：跟 court-watcher 风格一致的 bash 包装
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/bin/gitea-watcher`
- **关键内容**：参考 `bin/court-watcher` 第 1~30 行的 shebang + env + log 风格；exec `python -m court_mcp.gitea_watcher loop`
- **依赖**：T-12-10
- **验收命令**：`./bin/gitea-watcher --once && echo OK`
- **估算**：小（~0.5h）

#### T-12-14 · launchd plist 模板

- **目标**：在 macOS 启动时自启 gitea-watcher
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/docs/launchd/ai.k2lab.gitea-watcher.plist.template`
- **关键内容**：
  - Label: `ai.k2lab.gitea-watcher`
  - ProgramArguments: `/Users/wjx/Desktop/K2Work/agent-court/bin/gitea-watcher`
  - StartInterval: 30
  - StandardOutPath: `~/Library/Logs/gitea-watcher.out.log`
  - StandardErrorPath: `~/Library/Logs/gitea-watcher.err.log`
  - EnvironmentVariables: `K2LAB_GIT_TOKEN` 占位（fallback 链）+ `COURT_ROOT`
- **依赖**：T-12-13
- **验收命令**：
  ```bash
  launchctl load ~/Library/LaunchAgents/ai.k2lab.gitea-watcher.plist && \
    launchctl list | grep gitea-watcher
  ```
- **估算**：小（~1h）

#### T-12-15 · 错误兜底（连续失败通知 + token 失效退出）

- **目标**：守护进程不能疯狂重试拖垮自己 / 触发 rate limit
- **产出文件**：扩展 `gitea_watcher.py`，新建 `~/.agent-court/gitea-watcher/error-state.json` 记录连续失败计数
- **行为**：
  - 401 → `osascript -e 'display notification "Gitea token failed" with title "agent-court"'` → 退出码 78（EX_CONFIG）
  - 5xx / 网络错误连续 5 次 → 同上通知，退出码 75（EX_TEMPFAIL）
  - 单次失败 → 记 error-state.json，下次循环继续
- **依赖**：T-12-10
- **验收命令**：用错误 token 跑 `python -m court_mcp.gitea_watcher run-once`，看到通知并 exit 78
- **估算**：小（~1.5h）

---

### M4 · 审理 skill `shenli`（2 人日）

#### T-12-16 · skill 放置位置探查 + SKILL.md 骨架

- **目标**：先确认本仓库 skill 规范（agent-court 是否已有 `.claude/skills/<name>/SKILL.md` 还是 `.claude/skills/<name>.md`），保持风格一致
- **产出文件**（按探查结果二选一）：
  - `/Users/wjx/Desktop/K2Work/agent-court/.claude/skills/shenli/SKILL.md` 或
  - `/Users/wjx/Desktop/K2Work/agent-court/.claude/skills/shenli.md`
- **骨架内容**：name / description / when_to_use / trigger_keywords / 输入输出约定
- **依赖**：无
- **验收命令**：`cat .claude/skills/shenli/SKILL.md | head -30`
- **估算**：小（~0.5h）

#### T-12-17 · 审理决策逻辑文档化

- **目标**：把 §3.1 决策表写成 skill 内部的"判断步骤"，让 LLM 能机械执行
- **产出文件**：同 T-12-16 文件中追加章节 `## 决策流程`
- **关键内容**：
  - Step 1: 检查标签（wontfix/duplicate → REJECT）
  - Step 2: 检查必填字段（标题 / body / 仓库归属）
  - Step 3: 检查验收标准（关键词正则：`验收 | 完成定义 | 如何验证 | acceptance`）
  - Step 4: 输出 JSON
- **依赖**：T-12-16
- **估算**：小（~1h）

#### T-12-18 · agent_team_plan 生成逻辑

- **目标**：shenli 决定 GO 后，要给 court.yaml 推荐一份 roles 配置
- **产出**：同 SKILL.md 中新增 `## agent_team_plan 生成规则`
- **规则示例**：
  - label 含 `frontend` → roles 加 `dev-frontend`（cli=claude，model=sonnet-4.6）
  - label 含 `backend` 或 body 提到 `Flask/Express/Gin` → 加 `dev-backend`
  - 任何 GO → 至少含 `foreman` + `dev` + `qa`
  - 默认 cli=claude，可被 issue body 中 `#cli: codex` 指令覆盖
- **JSON 输出 schema**：
  ```json
  {
    "decision": "GO" | "NEED_INFO" | "REJECT",
    "court_project_name": "issue-k2lab-moras-finder-123",
    "agent_team_plan": {
      "roles": [
        {"name": "foreman", "cli": "claude", "model": "sonnet-4.6"},
        {"name": "dev", "cli": "claude", "model": "sonnet-4.6", "work_dir": "/path/to/repo"},
        {"name": "qa", "cli": "claude"}
      ],
      "dispatch_message": "请按 issue #123 完成 X，验收标准：Y。完成后 git push 并回 PR 链接。"
    },
    "missing_info": ["..."],     // 仅 NEED_INFO
    "reject_reason": "..."        // 仅 REJECT
  }
  ```
- **依赖**：T-12-17
- **估算**：中（~2h）

#### T-12-19 · shenli 后续动作 hooks（GO/NEED_INFO/REJECT 三分支落地）

- **目标**：skill 内文描述清楚每个 decision 后该调什么命令（守护进程读 JSON 后按描述执行）
- **产出**：同 SKILL.md `## 后续动作`
- **三分支命令**：
  - **GO**:
    ```bash
    # 1. 生成 court.yaml + prompts
    bin/migrate-to-court --new <court_project_name> --plan <agent_team_plan.json>
    # 2. 起 court
    bin/court-up <court_project_name>
    # 3. 派活
    # 通过 MCP: dispatch_to_foreman(<court_project_name>, <dispatch_message>)
    ```
  - **NEED_INFO**:
    ```bash
    python -m court_mcp.gitea_client comment --repo X --num N --body "<missing_info 模板>"
    ```
  - **REJECT**:
    ```bash
    python -m court_mcp.gitea_client comment --repo X --num N --body "<reject_reason>"
    python -m court_mcp.gitea_client transition --repo X --num N --state closed
    ```
- **依赖**：T-12-18、M1 全部
- **估算**：小（~1.5h）

#### T-12-20 · bin/migrate-to-court 扩展 `--new` flag

- **目标**：现有 `migrate-to-court` 只把已有目录注册为 court，需要支持"从零生成"
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/bin/migrate-to-court`（扩展，不重写）
- **关键改动**：新增 `--new <name> --plan <json_file>` 模式，生成最小 court.yaml + prompts/*.md
- **依赖**：T-12-18（plan schema 确定）
- **验收命令**：
  ```bash
  bin/migrate-to-court --new issue-test --plan /tmp/plan.json && \
    ls ~/.agent-court/projects/issue-test/court.yaml
  ```
- **估算**：中（~2h）

#### T-12-21 · 回执监听胶水（foreman done → comment 回 issue）

- **目标**：court 跑完后 foreman 写 upstream/inbox，gitea-watcher 要把回执转成 issue 评论
- **产出文件**：扩展 `gitea_watcher.py`，新增 `_drain_upstream_inboxes()` 在主循环里跑
- **关键逻辑**：
  - 遍历 `~/.agent-court/projects/issue-*/bus/upstream/inbox/*.md`
  - 解析 frontmatter 找出对应 issue（项目名解析回 `repo#num`）
  - 调 `comment_on_issue` 发出
  - 移到 `.done/`
- **依赖**：T-12-10、T-12-03
- **验收命令**：手动往 upstream inbox 投一条，看 issue 上是否出现评论
- **估算**：中（~2h）

---

### M5 · 集成胶水 + 文档（1 人日）

#### T-12-22 · `docs/issue-driven-workflow.md`

- **目标**：把 PR-12 的使用手册写完整
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/docs/issue-driven-workflow.md`
- **必含章节**：
  1. 粒度迁移说明（旧的 court-send vs 新的 issue-driven）
  2. 启停命令（`launchctl load/unload` + `bin/gitea-watcher --once`）
  3. token 来源链（keychain → env → netrc）
  4. 审理决策表（拷自 §3.1）
  5. 故障排查（401 / rate limit / pending-shenli 卡住 / court 起不来）
  6. 安全注意（不落盘 token、不擅自 push 公开仓库）
- **估算**：小（~2h）

#### T-12-23 · README.md 补"工单驱动模式"章节

- **目标**：让 onboard 同事第一眼看到新模式
- **产出**：`/Users/wjx/Desktop/K2Work/agent-court/README.md` 在现有架构图后追加 1 章
- **关键内容**：30 行内 ASCII 流程图（拷 §2 简化版） + 链接到 `docs/issue-driven-workflow.md`
- **估算**：小（~0.5h）

#### T-12-24 · PR-8~11 路线图加交叉引用

- **目标**：避免后续 PR-8 复核环节与 PR-12 自动跑撞车
- **产出**：`/Users/wjx/Desktop/K2Work/agent-court/.claude/plan/pr-8-to-11-monitor-memory-roadmap.md` 末尾追加
  ```markdown
  ## PR-12 工单驱动模式（外部入口）

  详见 .claude/plan/issue-driven-court.md。

  与本路线图的衔接：
  - PR-12 把"上游 LLM 派活"换成"Gitea issue 派活"，shenli 替代人工拍板
  - PR-8 监控窗仍然有用：监控 issue → court 派出的所有内部 bus 消息
  - PR-9 二段复核可作为 shenli GO 后的可选保险层（默认关闭，遇可疑 PR 触发）
  - PR-10 静态记忆 / PR-11 动态记忆继续按计划落地，shenli 可以 cat 这些文件辅助决策
  ```
- **估算**：小（~0.3h）

#### T-12-25 · `.gitignore` 加 seen-issues.json + pending-shenli/

- **目标**：防止 home dir 软链进 repo 被误提交（符合 §2.2 黑名单铁律）
- **产出**：`/Users/wjx/Desktop/K2Work/agent-court/.gitignore` 追加
  ```
  # PR-12 gitea-watcher 本地状态（绝不入库）
  **/seen-issues.json
  **/pending-shenli/
  **/gitea-watcher/error-state.json
  ```
- **估算**：小（~0.2h）

---

### INT · 集成测试（0.5 人日）

#### T-12-26 · e2e 验收脚本

- **目标**：一条命令验整链路，对应 §9 验收清单
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/tests/e2e/test_issue_driven.sh`
- **流程**：
  1. 用 `gitea_client` API 在 `K2Lab/agent-court-test` 创建一个 issue，body 含明确验收标准，assign 给自己
  2. 启动 `bin/gitea-watcher --once`
  3. 等 60s
  4. 断言 `~/.agent-court/gitea-watcher/seen-issues.json` 出现该 issue
  5. 断言 `tmux ls` 出现 `court-k2lab-agent-court-test-<num>` session
  6. 断言 issue 评论数 ≥ 1（foreman 回执）
  7. 清理：close issue + kill tmux + 删 court 目录
- **关键约束**：必须用 K2Lab 下专用测试仓 `agent-court-test`（如不存在先建一个），不能往生产仓打 issue
- **依赖**：所有前面任务
- **验收命令**：`bash tests/e2e/test_issue_driven.sh && echo PASS`
- **估算**：中（~3h）

#### T-12-27 · smoke 子集（无外部依赖）

- **目标**：CI 友好的快速版（不真打 Gitea）
- **产出文件**：
  - `/Users/wjx/Desktop/K2Work/agent-court/tests/smoke/test_issue_driven_smoke.sh`
- **覆盖**：
  - `python -m court_mcp.gitea_client --help` 不崩
  - `bin/gitea-watcher --help` 不崩
  - shenli SKILL.md 存在且 frontmatter 合法
  - gitea_watcher.py import 不崩
- **估算**：小（~0.5h）

---

## 5. 依赖关系图

```
M1 (Gitea client)
 ├─ T-12-01 (creds) ──┐
 ├─ T-12-02 (read) ───┼──┐
 ├─ T-12-03 (write) ──┤  │
 ├─ T-12-04 (CLI) ────┤  │
 └─ T-12-05 (test) ───┘  │
                         │
                         ├──> M2 (MCP tools T-12-06~09)  ← 可与 M3 并行
                         │
                         └──> M3 (watcher)
                                ├─ T-12-10 (loop) ──┐
                                ├─ T-12-11 (pending) │
                                ├─ T-12-12 (shenli glue) ────┐
                                ├─ T-12-13 (bash) ──┤        │
                                ├─ T-12-14 (plist) ─┘        │
                                └─ T-12-15 (errors)           │
                                                              │
M4 (shenli skill)                                             │
 ├─ T-12-16 (skeleton) ──┐                                    │
 ├─ T-12-17 (decision) ──┤                                    │
 ├─ T-12-18 (team plan) ─┼────────────────────────────────────┤
 ├─ T-12-19 (hooks) ─────┤                                    │
 ├─ T-12-20 (migrate) ───┘                                    │
 └─ T-12-21 (回执) ───────── needs M1 + M3.T-12-10 ──┐         │
                                                     │         │
M5 (docs/glue) — T-12-22~25  非阻塞，可与 M1~M4 并行 │         │
                                                     ▼         ▼
                                          INT (T-12-26 e2e) ←─┘
                                          INT (T-12-27 smoke)
```

### Critical Path（最长依赖链）

```
T-12-01 → T-12-02 → T-12-10 → T-12-11 → T-12-12 → T-12-21 → T-12-26
 (1.5h)   (3h)     (3h)      (1.5h)    (2h)      (2h)      (3h)
                                       ↑
                              T-12-18 (2h) 必须先于此
```

**关键路径估算**：约 2 人日（前提是无返工）

### 并行机会

| 并行组 | 内容 |
|---|---|
| Day 1 上午 | T-12-01（凭证） ‖ T-12-16（shenli 骨架） ‖ T-12-23（README） |
| Day 1 下午 | T-12-02、03（client）‖ T-12-17、18（shenli 决策） |
| Day 2 上午 | T-12-06~09（MCP tools）‖ T-12-10、11（watcher 骨架） |
| Day 2 下午 | T-12-12、13、14（watcher 完工） ‖ T-12-22（docs） |
| Day 3 | T-12-19、20、21 收尾 |
| Day 4 | INT |

---

## 6. PR 拆分建议

**建议**：**单 PR PR-12 一次性合**，不拆 a/b/c。理由：

| 不拆理由 | 说明 |
|---|---|
| 强耦合 | M1（client）、M3（watcher）、M4（shenli）三者缺一不能跑通 e2e |
| 单 reviewer | 当前 agent-court 只有用户一个 reviewer，拆分增加 context-switch 开销 |
| 不动生产 | 全部新文件 + 现有文件最小侵入（仅 server.py 追加 + migrate-to-court 加 flag），冲突面小 |

**回退方案**：如果 review 发现 PR 体积 > 1500 行，按以下顺序拆：
- **PR-12a**: M1 + M2（gitea client + MCP tools，独立可用）
- **PR-12b**: M3 + M4（watcher + shenli skill）
- **PR-12c**: M5 + INT（文档 + 测试）

---

## 7. 风险清单与缓解

| # | 风险 | 影响 | 概率 | 缓解措施 |
|---|---|---|:-:|---|
| R1 | **Gitea PAT 过期或被吊销** | 守护进程持续 401，可能触发 rate limit ban | 中 | T-12-15：连续 401 立即 exit + macOS notification；docs/issue-driven-workflow.md 给"如何刷新 keychain token"操作手册 |
| R2 | **issue 风暴**（一次指派 50+ issue 给我） | 50 个 tmux session + 50 个 LLM 进程 → 机器卡死 | 低 | gitea_watcher 加 `MAX_CONCURRENT_COURTS=5` 软上限；超过仅 NEED_INFO 留住不 spawn |
| R3 | **agent team 自动 push 出问题**（如改了不该改的仓库） | 公开 push 错代码，git 历史污染 | 中 | shenli 在 dispatch_message 里硬编码"先 dry-run + 改完先 commit 不要 push"；与 K2Work 规范"commit/push 都单独问"对齐；GO 决策时把 work_dir 锁死到 issue 关联的单一仓库 |
| R4 | **tmux session 名冲突**（issue 号撞了 court-send 手工 court） | 起 court 失败或 attach 到错的 session | 低 | session 名加固定前缀 `court-issue-`，与手工 court 物理隔离；migrate-to-court --new 启动前 `tmux has-session` 检查 |
| R5 | **Gitea rate limit**（默认 60 req/min） | 30s 轮询正常用不到，但 list+detail+comment 链路可能爆 | 低 | client 内置 ETag/`If-Modified-Since`（list_assigned_issues 用 `since` 参数）；连续 429 → 退避 2× |
| R6 | **shenli 误判 GO**（信息不够也 GO） | 浪费 court 资源 + 把垃圾 PR 推上去 | 中 | shenli 决策时强制要求 `verification_criteria` 字段非空才 GO；用户可在 issue 加 `:require-clarification:` label 强制 NEED_INFO |
| R7 | **launchd 子进程取不到 keychain** | watcher 启动即崩 | 中 | T-12-14 plist 加 `K2LAB_GIT_TOKEN` env 占位；docs 写明若 keychain 不可用需在 plist 写 token |
| R8 | **本机 fork 误进 git push 流** | 用户偏好"不擅自改别仓库代码"被打破 | 低 | shenli 决策 GO 时校验：issue 所在 repo 必须在本机 `~/Desktop/K2Work/<name>` 已 clone，否则降级 NEED_INFO 评论"请先在本机 clone 仓库" |

---

## 8. 验收清单

对齐用户的 7 条验收标准（推断自 §M1~M5 + INT 要求）：

| # | 验收点 | 对应任务 | 验收命令 |
|:-:|---|---|---|
| 1 | 凭证从 Keychain 取，不落盘 | T-12-01 | `grep -r "K2LAB_GIT_TOKEN\|password\s*=" mcp/ \| grep -v fallback` 应为空 |
| 2 | MCP 暴露 4 个新工具 | T-12-06~09 | `python -c "from mcp.court_mcp.server import mcp; print([t.name for t in mcp.list_tools()])"` 含 4 个 |
| 3 | gitea-watcher 可单跑、可 launchd 跑 | T-12-10、13、14 | `bin/gitea-watcher --once && launchctl list \| grep gitea-watcher` |
| 4 | shenli skill 输出严格 JSON | T-12-16~19 | 喂一份 fixture issue.md，断言输出 `python -c "import json; json.loads(open('out.json').read())"` 不崩 |
| 5 | GO 链路全自动跑通 | T-12-19、20、26 | T-12-26 e2e 脚本第 5 步断言 tmux session 出现 |
| 6 | NEED_INFO / REJECT 正确评论并不 spawn | T-12-19 | 给 issue 加 `wontfix` label 后重新轮询，断言 issue 已 closed 且无 court project |
| 7 | 回执自动写回 issue 评论 | T-12-21、26 | T-12-26 第 6 步断言 issue 评论数 ≥ 1 |

---

## 9. 本次明确不做的范围

| 不做 | 理由 |
|---|---|
| ❌ Gitea webhook 接收端 | D3：开发机无公网回调；改用轮询 |
| ❌ 跨平台支持（GitHub / GitLab） | 本期只对接 git.k2lab.ai；GiteaClient 接口设计预留可扩展，但不实现 |
| ❌ Web Dashboard（issue 列表 UI） | 监控窗 PR-8 已规划，本期复用 issue 自身页面 |
| ❌ 二段复核（GO 前再过一遍人） | PR-9 路线图；本期靠 shenli 单段决策 + R3 缓解措施 |
| ❌ 多账户支持（一台机器轮询多个 Gitea 用户） | 本期假设单用户单 keychain entry |
| ❌ 历史 issue 回填 | watcher 启动时只看 since=now，不补跑老 issue |
| ❌ 修改 PR-7 onboard 流程 | 不动既定路线；PR-12 是独立新分支 |
| ❌ shenli 跑 LLM 评估的 prompt 工程深度调优 | 本期出可用版，后续 PR 单独迭代 prompt |

---

## 10. 备注

- **首次落地前必须人工执行的事**：
  1. 在 git.k2lab.ai 创建一个 `K2Lab/agent-court-test` 仓（如果不存在），用于 T-12-26 e2e
  2. 确认 keychain 中 `git.k2lab.ai` 的 token 权限含 `repo:read` + `repo:write`（评论需要）+ `issue:write`（transition 需要）
  3. 跑一次 `bin/gitea-watcher --once` 手工预热，避免 launchd 首跑遇 keychain GUI 弹窗
- **回滚方案**：`launchctl unload ai.k2lab.gitea-watcher.plist` 即停掉自动派活；现有 court-send 手动派活路径不受影响

---

**总工时再次确认**：M1(1.5d) + M2(0.5d) + M3(2d) + M4(2d) + M5(1d) + INT(0.5d) = **7.5 人日**（含必要的 review/返工 buffer，区间 6.5 ~ 8 人日）
