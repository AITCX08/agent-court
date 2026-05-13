[中文](./cc-connect-bridge.md) | **English**

# Bridging a messaging platform → agent-court

`agent-court` doesn't ship a messaging bridge — it stops at the MCP layer.
But you can plug nearly any messaging platform (WeChat, Telegram, Slack,
Discord, …) on top by running a bridge that:

1. Exposes itself as a chat to your messaging app.
2. Forwards each inbound message to an LLM CLI that has the `agent-court`
   MCP server registered.

[`cc-connect`](https://github.com/chenhg5/cc-connect) is one such bridge.
This doc shows a minimal config.

> Nothing in this doc requires `agent-court` — these are just notes on
> using a third-party bridge with it. See cc-connect's own README for
> auth / token setup specific to your platform.

## Pattern A — Claude Code (or another `agent.type = "claudecode"` CLI)

The simplest path. The bridge launches Claude Code, Claude Code already
has the agent-court MCP server registered (via `claude mcp add -s user`),
and the rest is up to you.

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
# A neutral working directory. The MCP server is registered at user scope,
# so the actual path doesn't matter for tool access.
work_dir = "/path/to/some/dir"
mode = "bypassPermissions"

[[projects.platforms]]
type = "<your-platform>"   # e.g. telegram, slack, weixin, ...

[projects.platforms.options]
# platform-specific config — see cc-connect docs
```

Workflow: a user message arrives → cc-connect spawns Claude Code with
that message as the prompt → Claude Code decides which tool to call (e.g.
`dispatch_to_foreman`) → court-mcp writes the file → court-watcher routes
it → the chosen role works → result eventually comes back through
`read_upstream_inbox`.

## Pattern B — ACP-compatible assistant

If your upstream assistant supports ACP (Agent Client Protocol over
stdio), point cc-connect at it directly:

```toml
[projects.agent]
type = "acp"

[projects.agent.options]
work_dir = "/path/to/some/dir"
command = "/path/to/your/agent"
args = ["acp"]
```

Whatever agent you use needs to have the `agent-court` MCP server
configured on its end. Implementation varies by agent.

## Hint the upstream LLM about your courts

The MCP server publishes tool *signatures*, but the upstream LLM still
needs to *know* it should call them. Add a short note to the relevant
project's `CLAUDE.md` / system prompt / config:

```markdown
## agent-court

You have the `agent-court` MCP server attached. Call its tools when the
user wants something done in one of the local courts:

| User says | Call |
|---|---|
| "what projects do I have" | `list_projects()` |
| "send X to <project>'s foreman" | `dispatch_to_foreman(project, message)` |
| "send X to <project>'s backend" | `dispatch_to_foreman(project, message, target_role="backend")` |
| "what's happening in <project>" | `query_court_status(project)` |
| "any replies from <project>" | `read_upstream_inbox(project)` |

If unsure which project, call `list_projects()` first.
```

## Caveats

- The bridge typically spawns one upstream LLM session per chat thread,
  not one per message. State across messages in the same thread *is*
  shared by the upstream LLM but **not** by the courts. Each court
  conversation is reconstructed from its `bus/` filesystem each time.
- Tool calls + their results are surfaced into the chat by some bridges
  as verbose intermediate messages. That's a bridge UI issue, not
  agent-court behaviour. Configure the bridge to suppress intermediate
  events if your platform supports it.
- If the upstream LLM's gateway / provider has bugs handling
  `tool_result` follow-ups, you'll see the symptoms here (empty
  responses after a tool call). Switch to a known-good provider or pick
  a different upstream client.
