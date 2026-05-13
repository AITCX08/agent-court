**中文** | [English](./cc-connect-bridge.en.md)

# 把消息平台桥接到 agent-court

`agent-court` 不自带消息桥 —— 它只暴露到 MCP 这一层。
但是你可以在上面叠一个桥，把几乎任何消息平台（微信、Telegram、Slack、
Discord 等等）接进来，只要这个桥能：

1. 在消息平台一侧表现成一个聊天对端；
2. 把每条入站消息转发给一个**已经注册了 agent-court MCP server 的 LLM CLI**。

[`cc-connect`](https://github.com/chenhg5/cc-connect) 就是这样一个桥。
这份文档展示最小可用配置。

> 这份文档里的内容都不依赖 `agent-court` 本身 —— 只是用第三方桥配
> 它的笔记。各平台的认证 / token 配置请参考 cc-connect 自己的 README。

## 模式 A —— Claude Code（或其他 `agent.type = "claudecode"` 的 CLI）

最简路径。桥启动 Claude Code，Claude Code 已经把 agent-court MCP server
注册过了（通过 `claude mcp add -s user`），剩下交给你。

```toml
# ~/.cc-connect/config.toml
language = "en"

[log]
level = "info"

[[projects]]
name = "court-bridge"
admin_from = "*"

[projects.agent]
type = "claudecode"

[projects.agent.options]
# 一个中立的工作目录。MCP server 是 user 级注册的，
# 所以这里的路径不影响工具能否被调用。
work_dir = "/path/to/some/dir"
mode = "bypassPermissions"

[[projects.platforms]]
type = "<your-platform>"   # 比如 telegram / slack / weixin ...

[projects.platforms.options]
# 平台特定配置 —— 详见 cc-connect 文档
```

工作流：用户消息抵达 → cc-connect 起一个 Claude Code，把消息作为
prompt 喂进去 → Claude Code 判断该调哪个工具（比如
`dispatch_to_foreman`）→ court-mcp 写文件 → court-watcher 路由 →
对应 role 干活 → 结果最终通过 `read_upstream_inbox` 回来。

## 模式 B —— ACP 兼容的助手

如果你上游用的 assistant 支持 ACP（Agent Client Protocol，跑在 stdio 上），
直接让 cc-connect 指向它：

```toml
[projects.agent]
type = "acp"

[projects.agent.options]
work_dir = "/path/to/some/dir"
command = "/path/to/your/agent"
args = ["acp"]
```

不管用哪种 agent，你都得自己负责让它配上 `agent-court` MCP server。
具体怎么配看各 agent 的文档。

## 提示上游 LLM 知道你有哪些 court

MCP server 只发布工具**签名**，上游 LLM 还需要**知道**自己应该去调它们。
在对应项目的 `CLAUDE.md` / 系统提示 / 配置文件里加一段小提示：

```markdown
## agent-court

你已经接入了 `agent-court` MCP server。当用户想让某个本地 court 干活时，
调用它的工具：

| 用户说 | 调用 |
|---|---|
| "我有哪些项目" | `list_projects()` |
| "把 X 发给 <project> 的 foreman" | `dispatch_to_foreman(project, message)` |
| "把 X 发给 <project> 的 backend" | `dispatch_to_foreman(project, message, target_role="backend")` |
| "<project> 那边怎么样了" | `query_court_status(project)` |
| "<project> 有回信吗" | `read_upstream_inbox(project)` |

如果不确定该用哪个 project，先调 `list_projects()` 看一眼。
```

## 注意点

- 桥通常是**每个聊天线程**起一个上游 LLM 会话，不是每条消息一个。
  同一聊天线程里跨消息的状态是上游 LLM 自己维护的，court 这边**不维护** ——
  每次 court 的会话都是从 `bus/` 文件系统重建的。
- 有些桥会把工具调用 + 工具结果当成中间消息直接抖到聊天界面里。
  这是桥的 UI 问题，不是 agent-court 的行为。如果平台允许，配置桥
  把中间事件抑制掉就行。
- 如果上游 LLM 的网关/服务商在处理 `tool_result` 后续消息时有 bug，
  你会在这里看到症状（工具调用之后回复空白）。换一个已知稳定的
  服务商，或者换一个上游 client。
