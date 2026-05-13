[中文](./ARCHITECTURE.md) | **English**

# Architecture

This document walks through how a single message moves through `agent-court`
end to end. If `README.md` is the elevator pitch, this is the floor-by-floor
tour.

## Components

```
upstream LLM ── MCP stdio ── court-mcp ── filesystem ── court-watcher ── tmux ── role LLM CLI
```

Five processes, no daemons-of-daemons:

1. **Upstream LLM client** — anything that speaks MCP. Holds the
   relationship with the human user (terminal chat, messaging bridge,
   editor, whatever).
2. **`court-mcp` server** — Python (FastMCP). Started as a child of the
   upstream LLM when it loads its MCP servers. Translates tool calls into
   filesystem writes. Stateless.
3. **Filesystem bus** — `$COURT_ROOT/projects/<p>/bus/<role>/{inbox,outbox}/`.
   The only durable state in the system.
4. **`court-watcher` daemon** — Bash + `fswatch`. One per project. Routes
   `outbox/<file>.md` → `bus/<to>/inbox/<file>.md`, appends `event.log`,
   notifies the target tmux window.
5. **Role LLM CLI** — one per role, running inside its own tmux window
   under the project's tmux session. Reads its inbox, writes to its outbox,
   doesn't know about anything else.

## Message format

Every bus item is a single markdown file:

```markdown
---
from: foreman
to: frontend
ts: 2026-05-11T15:00:00+08:00
id: 7f3d2e1a
in_reply_to: 5a2c1b0d        # optional
---

Free-form body. This is what the receiving LLM reads.
```

- `id` is a random 8-char hex string. Globally unique enough; the watcher
  never deduplicates.
- `in_reply_to` lets you reconstruct conversation chains from
  `event.log`. It's a hint, not enforced.
- Filename: `<unix_ts>-<id>-<from>-to-<to>.md` — sortable, greppable.

## End-to-end example

Scenario: the upstream LLM (Claude Code) wants the `myproject` foreman to
verify yesterday's PR didn't break anything.

### 1. MCP tool call

Claude Code emits:

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

### 2. court-mcp writes a file

`court-mcp` receives the call and writes:

```
$COURT_ROOT/projects/myproject/bus/upstream/outbox/1715432400-7f3d2e1a-upstream-to-foreman.md
```

With frontmatter `from: upstream`, `to: foreman`, `id: 7f3d2e1a`.

The tool call returns immediately with the file path and id. No blocking
on the foreman.

### 3. court-watcher routes it

`fswatch` reports the new file. The watcher parses the frontmatter:

```
mv .../bus/upstream/outbox/<file>.md  →  .../bus/foreman/inbox/<file>.md
append .../shared/event.log:
  2026-05-11T15:00:00+08:00 | upstream -> foreman | id=7f3d2e1a | Verify PR #42 ...
tmux send-keys -t court-myproject:foreman "[notify] new inbox from upstream (id=7f3d2e1a): <file>"
```

### 4. Foreman reads, dispatches

The foreman's LLM (in tmux window `foreman`) sees the `[notify]` line,
reads `bus/foreman/inbox/<file>.md`, decides this is a backend job, writes:

```
.../bus/foreman/outbox/1715432410-a2b1c0d9-foreman-to-backend.md
  from: foreman
  to: backend
  in_reply_to: 7f3d2e1a
  body: "Backend, please run regression tests on the auth code path..."
```

Then `mv` the original to `bus/foreman/inbox/.done/`.

The watcher routes the new file the same way. Backend gets notified.

### 5. Backend works, replies

Backend's LLM does the work in its `work_dir` and writes its reply
addressed to `foreman` with `in_reply_to: a2b1c0d9`. Watcher routes it back.

### 6. Foreman summarises to upstream

Foreman reads backend's reply, decides the task is complete, writes:

```
.../bus/foreman/outbox/<file>.md
  from: foreman
  to: upstream
  in_reply_to: 7f3d2e1a
  body: "Done. PR #42 doesn't regress the auth flow. backend ran the suite at ..."
```

Watcher routes it to `bus/upstream/inbox/`.

### 7. Upstream LLM picks it up

Claude Code calls:

```json
{
  "method": "tools/call",
  "params": {
    "name": "read_upstream_inbox",
    "arguments": { "project": "myproject" }
  }
}
```

`court-mcp` returns the parsed message. Claude Code summarises it back to
the human.

## Design choices, briefly

### Why files?

- **Inspectable**: `cat`, `ls`, `grep` work.
- **Durable**: surviving a crash means inspecting the bus, not a memory
  dump.
- **Decoupled**: senders and receivers don't need to be alive at the same
  time. If the foreman's LLM is mid-response when a new dispatch arrives,
  the file sits in the inbox until the foreman gets to it.
- **Forkable**: you can copy a single `*.md` file to test a role's
  reaction to a known input.

### Why tmux + real CLIs?

- You can `attach`, watch, and *interject* mid-conversation by typing
  directly into a role's window.
- Cost / token usage is visible per role.
- Tools each CLI ships with (file edits, shell, etc.) are available
  inside each role without any wrapper.

### Why one watcher per project?

- One watcher = one tmux session = one project. They don't share state, so
  spinning up another project is `cp -r` and a second `court-up`.
- Each watcher's `fswatch` only walks one project's `bus/` tree → cheap.

### Why upstream is just another role

`upstream/outbox` and `upstream/inbox` are normal bus directories. The MCP
server writes to outbox and reads from inbox; it gets no special
treatment from the watcher. This means a human can also "be upstream" by
running `court-send --from upstream ...` and replies will land where the
MCP server would have looked.

## What this isn't

- **Not a scheduler.** Roles process inbox files in arrival order at their
  own pace. If you want fairness, throttling, retries, or priority queues,
  you build them on top.
- **Not a sandbox.** Each role runs with full host privileges of its CLI.
  If you need isolation, run each role inside Docker / a remote dev box
  and point its `work_dir` accordingly.
- **Not multi-machine.** The bus is the filesystem. To go cross-machine,
  swap the watcher for one that syncs `bus/` (Syncthing, etc.) or pubsubs
  the messages.

## Extending

- **Add a role**: edit `court.yaml`, drop a prompt file in `prompts/`,
  `court-down` then `court-up`.
- **Add a project**: copy `projects/example/`, change `session` + `project`
  + `work_dir`s, `court-up <new>`.
- **Custom CLI**: any LLM CLI that accepts a system-prompt flag works. Set
  `default_cli` in `court.yaml`, or override per-role with `cli`. Wrap
  exotic flags in a small shell script if needed.
- **Custom upstream**: write any client that speaks MCP; or skip MCP
  entirely and call `court-send --from upstream ...` from a webhook.
