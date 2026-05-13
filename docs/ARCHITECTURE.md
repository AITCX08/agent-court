**中文** | [English](./ARCHITECTURE.en.md)

# 架构

这份文档讲一条消息从头到尾在 `agent-court` 里的完整流动。如果说 `README.md`
是电梯介绍，这份就是逐层楼的实地游览。

## 组件

```
upstream LLM ── MCP stdio ── court-mcp ── 文件系统 ── court-watcher ── tmux ── role LLM CLI
```

五个进程，没有"守护守护进程"的嵌套：

1. **上游 LLM 客户端** —— 任何说 MCP 的程序。它持有"和人类用户"的关系
   （终端对话、消息桥、编辑器，随你）。
2. **`court-mcp` 服务** —— Python（FastMCP）。作为上游 LLM 的子进程启动，
   在它加载 MCP servers 时跟着起。把工具调用翻译成文件写入。无状态。
3. **文件系统总线** —— `$COURT_ROOT/projects/<p>/bus/<role>/{inbox,outbox}/`。
   系统里**唯一**的持久状态。
4. **`court-watcher` 守护进程** —— Bash + `fswatch`。每个 project 一份。
   把 `outbox/<file>.md` 路由到 `bus/<to>/inbox/<file>.md`，
   追加 `event.log`，并通过 tmux 通知目标窗口。
5. **Role LLM CLI** —— 每个 role 一份，跑在 project 的 tmux session 下的
   独立窗口里。读自己的 inbox，写自己的 outbox，不知道别人的存在。

## 消息格式

总线上的每一条都是一个 markdown 文件：

```markdown
---
from: foreman
to: frontend
ts: 2026-05-11T15:00:00+08:00
id: 7f3d2e1a
in_reply_to: 5a2c1b0d        # 可选
---

自由格式的正文。这是接收侧 LLM 实际读到的内容。
```

- `id` 是随机 8 字符 hex 串。全局唯一性"够用就行"；watcher 不做去重。
- `in_reply_to` 用来从 `event.log` 重建对话链。是提示性的，不强制。
- 文件名：`<unix_ts>-<id>-<from>-to-<to>.md` —— 可排序、可 grep。

## 端到端示例

场景：上游 LLM（Claude Code）想让 `myproject` 的 foreman 核对昨天的 PR 有没有破坏什么。

### 1. MCP 工具调用

Claude Code 发出：

```json
{
  "method": "tools/call",
  "params": {
    "name": "dispatch_to_foreman",
    "arguments": {
      "project": "myproject",
      "message": "Verify PR #42 didn't introduce regressions in the auth flow."
    }
  }
}
```

### 2. court-mcp 写一个文件

`court-mcp` 接到调用，写：

```
$COURT_ROOT/projects/myproject/bus/upstream/outbox/1715432400-7f3d2e1a-upstream-to-foreman.md
```

frontmatter 是 `from: upstream`、`to: foreman`、`id: 7f3d2e1a`。

工具调用立即返回文件路径和 id。**不阻塞**等 foreman。

### 3. court-watcher 路由它

`fswatch` 报新文件。watcher 解析 frontmatter：

```
mv .../bus/upstream/outbox/<file>.md  →  .../bus/foreman/inbox/<file>.md
追加 .../shared/event.log:
  2026-05-11T15:00:00+08:00 | upstream -> foreman | id=7f3d2e1a | Verify PR #42 ...
tmux send-keys -t court-myproject:foreman "[notify] new inbox from upstream (id=7f3d2e1a): <file>"
```

### 4. Foreman 读取后分派

foreman 的 LLM（在 tmux 窗口 `foreman` 里）看到 `[notify]` 行，
读 `bus/foreman/inbox/<file>.md`，决定这是 backend 的活，写：

```
.../bus/foreman/outbox/1715432410-a2b1c0d9-foreman-to-backend.md
  from: foreman
  to: backend
  in_reply_to: 7f3d2e1a
  body: "Backend, please run regression tests on the auth code path..."
```

然后 `mv` 原始消息到 `bus/foreman/inbox/.done/`。

watcher 用同样的方式路由新文件。backend 收到通知。

### 5. Backend 干活并回复

backend 的 LLM 在自己的 `work_dir` 里干完活，把回复写到 outbox，
收件人是 `foreman`，`in_reply_to: a2b1c0d9`。watcher 把它路由回去。

### 6. Foreman 汇总回上游

foreman 读 backend 的回复，判定任务完成，写：

```
.../bus/foreman/outbox/<file>.md
  from: foreman
  to: upstream
  in_reply_to: 7f3d2e1a
  body: "Done. PR #42 doesn't regress the auth flow. backend ran the suite at ..."
```

watcher 把它路由进 `bus/upstream/inbox/`。

### 7. 上游 LLM 取回

Claude Code 调用：

```json
{
  "method": "tools/call",
  "params": {
    "name": "read_upstream_inbox",
    "arguments": { "project": "myproject" }
  }
}
```

`court-mcp` 返回解析后的消息。Claude Code 把它向人类汇报。

## 简短的设计选择

### 为什么用文件？

- **可观察**：`cat`、`ls`、`grep` 都直接管用。
- **持久**：崩了之后能从总线翻状态，不用看内存快照。
- **解耦**：发送方和接收方不用同时活着。如果 foreman 的 LLM 正在
  回复别的事，新派的活就坐在 inbox 里等。
- **可分叉**：把一个 `*.md` 拷出来，就能针对已知输入复现某个 role 的反应。

### 为什么 tmux + 真的 CLI？

- 你可以 `attach`、观察、甚至**在对话中间直接键入插话**。
- token 用量按 role 可见。
- 每个 CLI 自带的工具（文件编辑、shell 等）在 role 内可直接使用，
  不需要任何 wrapper。

### 为什么每个 project 一个 watcher？

- 一个 watcher = 一个 tmux session = 一个 project。它们不共享状态，
  起一个新 project 就是 `cp -r` 加再来一次 `court-up`。
- 每个 watcher 的 `fswatch` 只扫一个 project 的 `bus/` 树 → 廉价。

### 为什么上游也只是普通 role？

`upstream/outbox` 和 `upstream/inbox` 是常规的总线目录。MCP server
往 outbox 写、从 inbox 读；watcher 不给它任何特殊待遇。这意味着
人类自己也可以"扮演 upstream"，用 `court-send --from upstream ...`
来发消息，回复也会落到 MCP server 原本去取的地方。

## 它**不是**什么

- **不是调度器**。各 role 按到达顺序、按自己的节奏处理 inbox。
  想要公平性、限流、重试、优先级队列，你自己叠在上面。
- **不是沙箱**。每个 role 都用对应 CLI 的全部宿主权限跑。如果你需要
  隔离，把 role 跑在 Docker / 远端开发机里，把它的 `work_dir`
  指过去。
- **不是跨机的**。总线就是文件系统。要跨机，要么把 watcher 换成
  能同步 `bus/` 的（Syncthing 等），要么把消息走 pubsub。

## 扩展

- **加一个 role**：改 `court.yaml`，往 `prompts/` 丢一份 prompt 文件，
  `court-down` 再 `court-up`。
- **加一个 project**：拷一份 `projects/example/`，改 `session` + `project`
  + 各 `work_dir`，然后 `court-up <new>`。
- **换 CLI**：任何接受 system-prompt 参数的 LLM CLI 都能用。
  在 `court.yaml` 里设 `default_cli`，或在 role 级用 `cli` 字段覆盖。
  CLI flag 不一样的可以包一层小 shell 脚本。
- **自定义上游**：写任何说 MCP 的客户端；或者完全不用 MCP，从 webhook
  里调 `court-send --from upstream ...`。
