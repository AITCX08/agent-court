**中文** | [English](./README.en.md)

# agent-court

> 一个本地的、迷你的多 Agent 编排器。你的 `agent-court` 安装下的每个
> project 都是一座小**法庭**（court），由若干 LLM CLI 进程组成 ——
> 一个角色一个 tmux 窗口 —— 通过文件系统消息总线协调工作。一个
> MCP server 把这条总线暴露给上游，让任何个人助手 LLM（Claude Code、
> Cursor、Zed 等等）都能往里派活。多台机器之间可以走 HTTP，配合
> ed25519 签名做联邦。

## 这个比喻

把你的 `agent-court` 安装当成一个小**朝廷**来想：

| 层 | 技术名 | 比喻 | 干什么 |
|---|---|---|---|
| 至上 | 人类（你） | 君王 | 下旨；审阅结果；最终拍板。 |
| 丞相 | 上游 LLM（Claude Code 等） | 丞相 / 助理 | 听君王说话，决定召唤哪个 court。 |
| 法庭 | `$COURT_ROOT/projects/<p>/` 下的一个 project | 府衙（一 project 一个） | 一个 tmux session 包着所有 role + 一间私邮间。 |
| 工头 | `foreman` 这个 role | 工头 | 接丞相派的活；拆成子任务交给百官。 |
| 百官 | `frontend` / `backend` / `devops` / … | 百官 | 在自己的文件里干自己的活。 |
| 邻邦 / 上司 / 下属 court | `peers.yaml` 里的一个对端 | 邻邦 | 你显式联邦过的、跑在别的机器上的另一个 `agent-court` project。 |

> **命名提醒**：英文里的 "peer" 是个一词两义 —— 既指"和 foreman
> 平级的 worker"，又指"被联邦的远端 court"。在代码里我们让 `peer`
> 专指**远端 court**；同级 court 在 `peers.yaml` 里用
> `relation: sibling` 表示（不是 `relation: peer`）。
> 详见下面的[联邦](#联邦可选)章节。

整套东西都是**可见**的：每个 role 是真的 tmux 窗口里跑着真的 CLI，
role 之间的每条消息都是一个你可以 `cat` 的 markdown 文件。

## 为什么

LLM CLI（Claude Code、Codex CLI、Cursor CLI 等等）**单个**都很强，
但孤独：它们不协作、不交接、不共享上下文。多 Agent 框架通常把所有
Agent 藏在同一个聊天框背后 —— 如果你**想看**它们在干什么、
想给某个卡住的 role **fork 一份 system prompt**、想在两轮之间
**塞一句人类提示**，那是错的方向。

`agent-court` 让 agent 保持可见（tmux）、持久（文件，不是内存）、
可观测（每个 project 一份 `event.log`）、可插拔（一个 MCP server
把整套东西暴露给任何会说 MCP 的程序）。当你要把活从一台机器上的
人接到另一台机器上的人时，可选的联邦层用 ed25519 签名 + HTTP POST
就行了。

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 你（人类）                                                           │
│   ↕ （任何 UI：终端、微信、Slack、网页 等等）                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────────┐
│ 上游 LLM（比如 Claude Code、Cursor、自研助手）                       │
│   - 会说 MCP                                                         │
│   - 挂着 agent-court MCP server                                      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ MCP（stdio JSON-RPC）—— 全本地访问
┌──────────────────────────────┴───────────────────────────────────────┐
│ court-mcp server（Python，FastMCP）                                   │
│  local : list_projects / dispatch_to_foreman / query_court_status     │
│          read_upstream_inbox                                          │
│  peer  : list_peers / dispatch_to_peer    ← 签名 + POST 给远端        │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 写 markdown 文件
┌──────────────────────────────┴───────────────────────────────────────┐
│ $COURT_ROOT/projects/<p>/bus/<role>/{inbox, outbox, inbox/.done}/    │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ fswatch 看到新文件
┌──────────────────────────────┴───────────────────────────────────────┐
│ court-watcher 守护进程                                                │
│   解析 frontmatter → mv 到目标 inbox → 追加 event.log                 │
│                    → tmux send-keys 通知目标窗口                      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────────┐
│ tmux session: court-<project>                                         │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐      │
│   │ foreman    │  │ frontend   │  │ backend    │  │ devops     │  …   │
│   │ (LLM CLI)  │  │ (LLM CLI)  │  │ (LLM CLI)  │  │ (LLM CLI)  │      │
│   └────────────┘  └────────────┘  └────────────┘  └────────────┘      │
└──────────────────────────────────────────────────────────────────────┘

可选的联邦（按 project，默认关）：

   机器 A / project foo                       机器 B / project foo
   ┌──────────────────────────┐               ┌──────────────────────────┐
   │ MCP: dispatch_to_peer    │ ed25519 签名   │ court-peer :8765 /inbox  │
   │   → POST /inbox          │──────────────▶│   校验签名               │
   │                          │               │   查 expose_roles        │
   │                          │               │   落到 bus/              │
   └──────────────────────────┘               └──────────────────────────┘
```

每条消息是一个 markdown 文件：

```markdown
---
from: foreman
to: frontend
ts: 2026-05-11T15:00:00+08:00
id: 7f3d2e1a
in_reply_to: 5a2c1b0d        # 可选
---

正文。自由的 markdown —— LLM 看到的就是这部分。
```

文件名：`<unix_ts>-<id>-<from>-to-<to>.md`。watcher 靠 YAML frontmatter
路由。回信把 `in_reply_to` 设成原消息 id 就能串成对话。

## 快速上手

### 前置条件

- macOS 或 Linux
- `tmux`、`fswatch`、`uuidgen` 在 `$PATH` 上
- `yq` —— 必须是**包装 `jq` 的 Python 版** (`pip install yq`)。
  `mikefarah/yq` 的 Go 版语法不兼容，会让 `bin/` 下的 shell 脚本崩。
- Python 3.10+（给 MCP server 和联邦守护进程用）
- 一个能接 `--append-system-prompt`、可选 `--model` 的 LLM CLI。
  默认是 `claude`（Anthropic 的 Claude Code），任何兼容的都行 ——
  在你 project 的 `court.yaml` 里设 `default_cli`，或者在 role 级
  用 `cli` 覆盖。

### 安装

```bash
# 1. clone
git clone https://github.com/YOUR_GH_USER/agent-court.git ~/agent-court
cd ~/agent-court

# 2. 把 bin/ 放进 PATH（bash/zsh）
echo 'export PATH="$HOME/agent-court/bin:$PATH"' >> ~/.zshrc
# fish:
#   fish_add_path --prepend $HOME/agent-court/bin

# 3. 装 MCP server（上游 LLM 通过它派活到 court 里）
cd mcp/court-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .

# 4. 建你的 court 主目录，把示例 project 拷进去
mkdir -p ~/.agent-court/projects
cp -r ~/agent-court/projects/example ~/.agent-court/projects/myproject
# 改 ~/.agent-court/projects/myproject/court.yaml 里的 work_dir 路径
```

### 跑起来

```bash
court-up myproject
```

这会起一个名叫 `court-myproject` 的 tmux session，里面每个 role 一个
窗口，每个窗口跑着 LLM CLI（已经把 role 的 system prompt 加载好了）。
`court-watcher` 守护进程在后台一起起来；日志在
`~/.agent-court/projects/myproject/logs/`。

停掉：

```bash
court-down myproject
```

### 从命令行发一条消息

```bash
court-send -p myproject --to foreman "审一下新的 auth 改动，需要的话分派给该跟进的角色"
```

foreman 的 claude 窗口会收到一行 `[notify]`，去读 inbox 然后反应。

### 接入 Claude Code（或任何 MCP 客户端）

```bash
# Claude Code：以 user scope 注册 court MCP server
claude mcp add -s user agent-court \
  $HOME/agent-court/mcp/court-mcp/.venv/bin/python \
  $HOME/agent-court/mcp/court-mcp/server.py

# 验证
claude mcp list   # 应该能看到 agent-court ✓ Connected
```

Claude Code 现在就能看到完整的本地 MCP 工具集：

| 工具 | 什么时候用 |
|---|---|
| `list_projects` | 用户提到某个 project 名字，你想知道有哪些可选。 |
| `dispatch_to_foreman(project, message, target_role?)` | 用户想让某个 court 里的人干件事。 |
| `query_court_status(project)` | 用户问 "`<project>` 现在怎么样？" |
| `read_upstream_inbox(project)` | 用户问 "`<project>` 有回信吗？"（foreman 回给上游的消息住这）。 |
| `list_peers(project)` | 用户问某个 project 的联邦状态。 |
| `dispatch_to_peer(project, peer_court_id, message, ...)` | 用户想把一件事转发给一个被联邦的 court。 |
| `grant_peer_access(project, peer_court_id, paths, ttl?)` | 用户想临时拓宽某个 peer 的 `attaches:` 能引用的范围。 |
| `grant_peer_tier(project, peer_court_id, target_tier, ttl?, consume_on_use?)` | 用户想临时把某个 peer 的 tier（比如 `tier_a` → `tier_c`）提一档，限时或限一条消息。 |
| `list_grants(project)` / `grant_info(project, id)` / `revoke_grant(project, id)` | 用户想查看或撤销一个未到期的 grant。 |

本地 MCP 工具有**完整的机器访问权** —— 它们在 `$COURT_ROOT/projects/<p>/`
下随便读写。受约束的那部分是联邦侧（见下一节）。

同样的形状也适用于 Cursor / Zed / 任何支持 MCP 的助手，或者你自己写的
Hermes 风格 agent —— 只要它能 spawn 一个 MCP stdio server。

## 联邦（可选）

默认是**关闭**的。每个 project 自己决定要不要接收来自联邦对端的入站
消息 —— 没有"全局开关"。

模型是**按 project 划分而非按机器划分**：`$COURT_ROOT/projects/<p>/`
下的每个 project 都有自己的密钥对、自己的 `peers.yaml`、自己的
`court_id`。同一台机器上的两个 project，互相**无法**推断对方存在
（不同的 key，独立的 peer 列表）。这是有意的隔离 —— "我替客户 A
做的活"不应该因为共用一台笔记本就泄漏到"我替客户 B 做的活"里。

给一个 project 打开联邦：

```bash
# 1. 给这个 project 生密钥对
court-keygen myproject
# → 打印公钥 + 指纹，分给对端

# 2. 改 court.yaml —— 把 federation: 块取消注释
$EDITOR ~/.agent-court/projects/myproject/court.yaml

# 3. 把远端 peer 加到这个 project 的 peers.yaml
$EDITOR ~/.agent-court/projects/myproject/peers.yaml
# （schema 见 projects/example/peers.example.yaml）

# 4. 给这个 project 起 receiver 守护进程
court-peer myproject
# → 默认监听 0.0.0.0:8765，接收 POST /inbox
```

当 `federation: enabled: false`（或整块都没写）时，`court-peer`
拒绝启动，`dispatch_to_peer` 返回 `{"error": "federation_disabled"}`。
把 flag 翻回 false，下一个入站请求就会生效 —— 不用重启。

入站消息在落到总线之前要过四道关：

1. **签名** —— 用这个 project `peers.yaml` 里对端的 `pub_key_b64`
   校验。签名不对 → 401。
2. **已知发件人** —— `from_court` 必须出现在这个 project 的
   `peers.yaml` 里。未知 → 403。
3. **角色白名单** —— `to:` 这个 role 必须在
   `federation.expose_roles` 里。默认是 `[foreman]`，也就是说
   外面只能打到 foreman，foreman 再在内部分派。不在名单 → 403。
4. **策略引擎**（PR-2）—— 见下一小节。

PR-1 落了网络 + 身份 + 角色白名单；PR-2 加了下面的策略层；后续
PR 会加 LLM 裁判、通过飞书/微信走的人工审批、IM 冗余。

### 策略引擎（PR-2）

签名 + 角色检查通过之后，每条入站消息都会被策略引擎打分并路由到
四种结局之一：

| 判定 | 落到 | 什么时候 |
|---|---|---|
| `auto_pass` | `bus/<peer>/inbox/` | tier_c peer、正文干净、attach 在允许清单里 —— 或者 tier_b 消息被 PR-3 的 LLM 裁判判通过 |
| `human_required` | `bus/<peer>/pending-approval/` | tier_a peer、敏感关键字、attach 不在 allow_paths 里 —— 或者 PR-3 LLM 裁判把 tier_b 升到 human_required |
| `denied` | `bus/<peer>/denied/` *（仅审计）* | attach 命中 deny 路径。**不会**到达 foreman。 |

PR-3 把真正的 LLM 接到了 tier_b 上。策略说 `judge` 时，守护进程
就用配好的 LLM CLI（默认 `default_cli`，比如 Claude Code）带上
内建的裁判 system prompt 去调，解析 JSON 判决，再用置信度阈值
过滤。任何失败（CLI 不在 PATH、超时、输出不可解析、置信度过低）
都**失败安全地回落到 `human_required`** —— 接收方不会比没有 PR-3
时更差。

配置分散在 project 内两个文件：

- **`court.yaml`** —
  - `federation.allow_paths` / `deny_paths` —— 限制入站消息
    `attaches:` 中能引用的路径 glob。
  - `federation.judge` —— tier_b 调哪个 CLI 来裁判，可选 `model`、
    可选 `prompt_file` 覆盖、`timeout_seconds`（默认 30）、
    `confidence_threshold`（默认 0.6）。`judge.cli` 没设时回落
    到顶层 `default_cli`。
- **`policy.yaml`** —— `default_tier:`（`tier_a`/`tier_b`/`tier_c`
  之一） + 可选 `sensitive_keywords:` 列表（追加到内建之后）。

`peers.yaml` 可以按 peer 钉死 `policy_tier:`；缺省时回落到
`policy.yaml` 的 `default_tier`。

**硬规则层（不能被 config 覆盖）**。命中
`**/.ssh/**`、`**/.env`、`**/id_rsa*`、`/etc/**`、
`**/credentials.json`、`**/secrets/**`、`**/.aws/**`、
`**/.kube/config` 的路径**永远**被拒。正文里含
`api_key`、`password`、`secret`、`token`、`sk-`、`AKIA` 等的
**永远**强制 `human_required`。

每个判定都会被追加到 `$COURT_ROOT/projects/<p>/logs/policy-log.jsonl`
方便审计。

完整端到端示例（含 `attaches:` 字段）见 [docs/lan-deployment.md](./docs/lan-deployment.md)。

### 临时授权（PR-4）

当 `allow_paths` 对某次一次性需求太窄（"Bob，帮我快速看一下
`notes/q2-plan.md`"），接收方可以发一张时效绑定、peer 绑定的
临时授权 —— 一个 sudo 时刻，而不是改 config。硬规则的 deny 仍然先赢；
授权只**加**权限，不会**减**任何东西。

两类授权，按 `grant_type` 区分：

| 类型 | 放宽什么 | 用在 |
|---|---|---|
| **path**（默认） | `allow_paths` | 你想放过的那个 attach 不在静态白名单里 |
| **tier** | 那个 peer 的 `policy_tier`（限一条 `--once` 或限时） | 想跳过一个已知放心批次的 judge / 人工审 |

硬编码的 deny、用户的 `deny_paths`、`HARDCODED_KEYWORDS` 永远还是
先赢。授权只能 *加* 能力，永远不能 *减*。

```bash
# Path 授权 —— Bob 30 分钟内可以 attach 任何 notes/ 下的东西
court-grant example bob "notes/**"
# 显式 TTL —— 接受 30m / 1h / 2h30m / 1d / 裸数字（秒）
court-grant example bob "shared/draft-*.md" --ttl 2h

# Tier 授权 —— 把 Bob 升到 tier_c 只用一次
court-grant example bob --tier tier_c --once

# Tier 授权 —— 升一个小时
court-grant example bob --tier tier_c --ttl 1h

court-grant example list
# STATE     T ID         PEER  EXPIRES                  HITS DETAIL
# active    P 4616c19a   bob   2026-05-13T22:53:00+...  0    notes/**
# active    T 7fa20bd8   bob   2026-05-13T23:00:00+...  0    →tier_c [once]

court-grant example info 4616c19a       # 完整记录 + 剩余时间 + 命中次数
court-grant example revoke 4616c19a
```

`T` 列：`P` 是 path 授权，`T` 是 tier 授权。`info` 显示 `state`、
`remaining`、`hit_count`、`last_hit_ts`，以及（once 授权的）
`consumed_ts`。

授权是 `$COURT_ROOT/projects/<p>/grants/` 下的 JSON 文件，**原子写入**，
读取时严格校验（过大 / 损坏的文件会被跳过并写入 `logs/peer-errors.log`
警告）。重启守护进程不会丢；`revoke` 直接删文件。从上游 LLM 看，
同一组面孔通过 `grant_peer_access` / `grant_peer_tier` / `grant_info` /
`list_grants` / `revoke_grant` 暴露。

每个授权入口对 `project` 参数都校验：必须是单一安全文件系统组件，
且解析后必须严格位于 `$COURT_ROOT/projects/` 之下。传
`project="../foo"` 直接报错，不会读到根目录之外。

完整双机演练见 [docs/lan-deployment.md](./docs/lan-deployment.md)。

## 目录布局

```
$COURT_ROOT/                                  # 默认 ~/.agent-court
├── projects/
│   └── myproject/
│       ├── court.yaml                        # project 配置（含 federation 块）
│       ├── peers.yaml                        # 这个 project 已知的对端
│       ├── policy.yaml                       # PR-2: tier + 敏感词（可选）
│       ├── identity/                         # 这个 project 的密钥对（0600/0644）
│       │   ├── priv.key
│       │   └── pub.key
│       ├── grants/                           # PR-4: 每个授权一份 JSON
│       │   └── <id>.json
│       ├── prompts/
│       │   ├── foreman.md
│       │   ├── frontend.md
│       │   └── ...                           # 每个 role 一份
│       ├── bus/
│       │   ├── foreman/{inbox,outbox,inbox/.done}/
│       │   ├── frontend/...
│       │   ├── backend/...
│       │   ├── upstream/...                  # MCP caller 的 outbox/inbox
│       │   ├── human/...                     # 你 CLI 发的消息落这里
│       │   └── <peer_court_id>/              # 入站 peer 消息，按判定分桶
│       │       ├── inbox/                    #   auto_pass + judge 落这
│       │       ├── pending-approval/         #   human_required 停这
│       │       └── denied/                   #   denied（仅审计，永远不投递）
│       ├── shared/event.log
│       └── logs/{watcher.log, peer-errors.log, policy-log.jsonl, watcher.pid}
```

仓库（本仓库）本身只装：
- `bin/` —— shell 脚本（`court-up`、`court-down`、`court-watcher`、
  `court-send`、`role-launch`、`court-keygen`、`court-peer`、`court-grant`）
- `mcp/court-mcp/` —— Python MCP server + peer 守护进程 + keygen
- `projects/example/` —— 一个可 fork 的示例 project（含被注释掉的
  `federation:` 块作为 schema 参考）
- `docs/` —— 补充文档（LAN 部署、cc-connect 桥接 等）

你**实际**的 court 跑在 `$COURT_ROOT` 下（默认 `~/.agent-court/`），
**在仓库外**。

## FAQ

### 为什么不直接用 sub-agent / 一个 agent 框架？

Sub-agent（Claude Code、AutoGen、CrewAI 等等里的）**替你**决定何时
派 worker，把它们藏在背景上下文里，干完拆掉。你没法 `tmux attach`
看 agent 思考，没法在任务中途 fork 它的 system prompt，消息图被
框架占有。

`agent-court` 更接近一个小操作系统：长期运行的 role 进程、文件系统
IPC、外部 watcher。抽象更糙，可观察性更好。

### 这玩意只用来写代码吗？

不。Role 是开放的。任何能用 system prompt 描述出来的都是合法 role：
拉趋势的研究员、把研究改成脚本的文案、读日志的分析师。配你想用的
任何 LLM CLI。

### 它和我现有的 LLM CLI 怎么相处？

`role-launch` 调你的 CLI 时带上 `--append-system-prompt <prompt file>`
和（可选）`--model <model>`。如果你的 CLI 用别的 flag，给这个 role
的 `cli` 字段指一个小包装脚本就行。

### 为什么每个 project 都要单独密钥对？我能不能跨 project 共用一个？

技术上**可以**，但设计上故意劝退。每个 project 一个 key 的意义
是："Alice 替客户 A 的工作"和"Alice 替客户 B 的工作"在网络上是
两座完全不同的法庭 —— Alice 替 project A 联邦过的对端，**不会**
因为这份信任，就能看到或派活到 project B。
**不同的 project = 不同的 `court_id` + 不同的 key + 不同的 `peers.yaml`**。
跨 project 共用密钥对会把这层隔离压垮。

### 成本上呢？

每个 role 是独立的 CLI 会话，上下文**不共享** —— 每个 role 都自己
重读 system prompt + 自己的 inbox。这是为换隔离付的代价。如果你
只有一个 project 在用，那就只起那一个。

## License

MIT。见 [LICENSE](./LICENSE)。

## 状态

早期。PR-1（HTTP + 身份 + 签名分发 + 角色白名单）、
PR-2（策略引擎 + 路径级 allow/deny + 敏感词过滤 + pending-approval 桶）、
PR-3（tier_b 的 LLM 裁判，失败安全回落 human_required）、
PR-4（sudo 风格临时授权 —— peer 绑定、时效绑定的授权，
要么放宽 `allow_paths`（path 授权），要么覆盖软层 tier
（tier 授权，可选 `--once` 语义）；走 `court-grant` + MCP；
针对路径穿越、原子写、严格 JSON 校验都做了加固）已经在跑，
带 150+ 测试。PR-5（多通道人工审批：终端 + 飞书 + 微信）
和 PR-6（IM 冗余）排在后面。欢迎报 bug 或提新 role 范式 —— 开 issue。
