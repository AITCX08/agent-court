[中文](./README.md) | **English**

# agent-court

> A tiny local multi-agent orchestrator. Each project under your `agent-court`
> installation is a small **court** of LLM CLI processes — one tmux window
> per role — coordinating through a filesystem message bus. An MCP server
> exposes the bus upstream so any personal-assistant LLM (Claude Code,
> Cursor, Zed, …) can dispatch work in. Multiple machines can federate over
> HTTP with ed25519-signed messages.

## The metaphor

Think of your `agent-court` installation as a small **government**:

| Layer | Technical name | Metaphor | What they do |
|---|---|---|---|
| Sovereign | the human (you) | 君王 | Issue intent; review results; final say. |
| Chancellor | upstream LLM (Claude Code, etc.) | 丞相 / 助理 | Listens to the sovereign, decides which court to call. |
| Court | a project under `$COURT_ROOT/projects/<p>/` | 府衙 (one per project) | A tmux session of roles + a private mailroom. |
| Foreman | the `foreman` role | 工头 | Receives the chancellor's dispatch; splits work across workers. |
| Workers | `frontend` / `backend` / `devops` / … | 百官 | Do the actual work in their own files. |
| Sibling / Parent / Child court | a peer in `peers.yaml` | 邻邦 / 上司 / 下属 | Another `agent-court` project on another machine you've explicitly federated with. |

> **Naming note.** "Peer" is overloaded: it means *both* a worker role
> alongside the foreman, *and* a federated remote court. Inside the code
> we keep `peer` for the remote-court meaning and use `relation: sibling`
> (not `relation: peer`) inside `peers.yaml` to refer to courts at the
> same level. See [Federation](#federation-optional) below.

Everything is *visible*: each role is a real tmux window running a real CLI,
and every message between roles is a markdown file you can `cat`.

## Why

LLM CLIs (Claude Code, Codex CLI, Cursor CLI, etc.) are *individually*
powerful but lonely. They don't coordinate, they can't hand off, and they
share no context. Multi-agent frameworks usually hide the agents behind a
single chat window — which is the wrong direction if you want to *watch*
what's happening, fork a frozen role's prompt, or feed in human nudges
between turns.

`agent-court` keeps agents visible (tmux), durable (files, not RAM),
inspectable (a single `event.log` per project), and pluggable (one MCP
server exposes the whole thing to anything that speaks MCP). When you
need to hand work between people on different machines, an opt-in
federation layer signs messages with ed25519 and POSTs them over HTTP.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ You (human)                                                           │
│   ↕ (any UI: terminal, WeChat, Slack, web, etc.)                      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────────┐
│ Upstream LLM (e.g. Claude Code, Cursor, a custom assistant)           │
│   - speaks MCP                                                        │
│   - has the agent-court MCP server attached                           │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ MCP (stdio JSON-RPC) — full local access
┌──────────────────────────────┴───────────────────────────────────────┐
│ court-mcp server (Python, FastMCP)                                    │
│  local : list_projects / dispatch_to_foreman / query_court_status     │
│          read_upstream_inbox                                          │
│  peer  : list_peers / dispatch_to_peer    ← signs + POSTs to remote   │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ writes markdown files
┌──────────────────────────────┴───────────────────────────────────────┐
│ $COURT_ROOT/projects/<p>/bus/<role>/{inbox, outbox, inbox/.done}/     │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ fswatch sees the new file
┌──────────────────────────────┴───────────────────────────────────────┐
│ court-watcher daemon                                                  │
│   parse frontmatter → mv to target inbox → append event.log           │
│                     → tmux send-keys notify target window             │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────┴───────────────────────────────────────┐
│ tmux session: court-<project>                                         │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐      │
│   │ foreman    │  │ frontend   │  │ backend    │  │ devops     │  …   │
│   │ (LLM CLI)  │  │ (LLM CLI)  │  │ (LLM CLI)  │  │ (LLM CLI)  │      │
│   └────────────┘  └────────────┘  └────────────┘  └────────────┘      │
└──────────────────────────────────────────────────────────────────────┘

Optional federation (per-project, default OFF):

   Machine A / project foo                    Machine B / project foo
   ┌──────────────────────────┐               ┌──────────────────────────┐
   │ MCP: dispatch_to_peer    │ ed25519 sig   │ court-peer :8765 /inbox  │
   │   → POST /inbox          │──────────────▶│   verify sig             │
   │                          │               │   check expose_roles     │
   │                          │               │   drop into bus/         │
   └──────────────────────────┘               └──────────────────────────┘
```

Each message is one markdown file:

```markdown
---
from: foreman
to: frontend
ts: 2026-05-11T15:00:00+08:00
id: 7f3d2e1a
in_reply_to: 5a2c1b0d        # optional
---

Body text. Free-form markdown — that's what the LLM reads.
```

Filename: `<unix_ts>-<id>-<from>-to-<to>.md`. The watcher uses the YAML
frontmatter to route. Replies set `in_reply_to` to chain a conversation.

## Quickstart

### Prerequisites

- macOS or Linux
- `tmux`, `fswatch`, `uuidgen` on `$PATH`
- `yq` — **the Python wrapper around `jq`** (`pip install yq`). The Go
  `yq` from `mikefarah/yq` has incompatible syntax and breaks the
  shell scripts in `bin/`.
- Python 3.10+ (for the MCP server and the federation daemon)
- An LLM CLI that accepts `--append-system-prompt` and optionally
  `--model`. The default is `claude` (Anthropic's Claude Code), but
  anything compatible works — set `default_cli` in your project's
  `court.yaml`, or override per-role with `cli`.

### Install

```bash
# 1. clone
git clone https://github.com/YOUR_GH_USER/agent-court.git ~/agent-court
cd ~/agent-court

# 2. put the bin/ on your PATH (bash/zsh)
echo 'export PATH="$HOME/agent-court/bin:$PATH"' >> ~/.zshrc
# fish:
#   fish_add_path --prepend $HOME/agent-court/bin

# 3. install the MCP server (so an upstream LLM can dispatch into courts)
cd mcp/court-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .

# 4. create your court home and copy the example project
mkdir -p ~/.agent-court/projects
cp -r ~/agent-court/projects/example ~/.agent-court/projects/myproject
# edit ~/.agent-court/projects/myproject/court.yaml to set real work_dir paths
```

### Run

```bash
court-up myproject
```

This brings up a tmux session `court-myproject` with one window per role,
each running its LLM CLI with the role's system prompt loaded. A `court-watcher`
daemon starts in the background; logs at `~/.agent-court/projects/myproject/logs/`.

To stop:

```bash
court-down myproject
```

### Send a message from the command line

```bash
court-send -p myproject --to foreman "review the new auth changes and dispatch to whoever needs to follow up"
```

The foreman's claude window will get a `[notify]` line, read the inbox,
and react.

### Connect to Claude Code (or any MCP client)

```bash
# Claude Code: register the court MCP server at user scope
claude mcp add -s user agent-court \
  $HOME/agent-court/mcp/court-mcp/.venv/bin/python \
  $HOME/agent-court/mcp/court-mcp/server.py

# Verify
claude mcp list   # should show agent-court ✓ Connected
```

Claude Code now sees the full local-MCP toolset:

| Tool | Use it when... |
|---|---|
| `list_projects` | The user mentions a project by name and you want to know what's available. |
| `dispatch_to_foreman(project, message, target_role?)` | The user wants someone in a court to do something. |
| `query_court_status(project)` | The user asks "what's happening in `<project>`?". |
| `read_upstream_inbox(project)` | The user asks "any updates from `<project>`?" (foreman's replies live here). |
| `list_peers(project)` | The user asks about federation status for a project. |
| `dispatch_to_peer(project, peer_court_id, message, ...)` | The user wants to forward something to a federated court. |
| `grant_peer_access(project, peer_court_id, paths, ttl?)` | The user wants to temporarily widen what a peer's `attaches:` may reference. |
| `grant_peer_tier(project, peer_court_id, target_tier, ttl?, consume_on_use?)` | The user wants to bump a peer's tier (e.g. `tier_a` → `tier_c`) for a window or a single message. |
| `list_grants(project)` / `grant_info(project, id)` / `revoke_grant(project, id)` | The user wants to inspect or kill an outstanding grant. |

Local MCP tools have **full machine access** — they read and write
anywhere under `$COURT_ROOT/projects/<p>/`. The restriction surface lives
on the federation side (next section).

Same shape works for Cursor, Zed, any MCP-aware assistant, or a custom
Hermes-style agent — anything that can spawn an MCP stdio server.

## Federation (optional)

Default is **off**. Each project decides for itself whether to accept
inbound messages from federated peers — there is no global switch.

The model is **project-scoped, not machine-scoped**: each project under
`$COURT_ROOT/projects/<p>/` has its own keypair, its own `peers.yaml`,
and its own `court_id`. Two projects on the same machine cannot infer
that the other exists (different keys, separate peer lists). This is
deliberate isolation — "my work for client A" should not leak into "my
work for client B" just because they share a laptop.

To enable federation for one project:

```bash
# 1. generate that project's keypair
court-keygen myproject
# → prints the public key + fingerprint to share with the other side

# 2. edit court.yaml — uncomment the federation: block
$EDITOR ~/.agent-court/projects/myproject/court.yaml

# 3. add the remote peer to that project's peers.yaml
$EDITOR ~/.agent-court/projects/myproject/peers.yaml
# (see projects/example/peers.example.yaml for the schema)

# 4. start the receiver daemon for that project
court-peer myproject
# → listens on 0.0.0.0:8765 by default, accepts POST /inbox
```

When `federation: enabled: false` (or the block is missing entirely),
`court-peer` refuses to start and `dispatch_to_peer` returns
`{"error": "federation_disabled"}`. Flipping the flag back to false
takes effect on the next inbound request — no restart needed.

Inbound messages go through four checks before they land in the bus:

1. **Signature** — verified against the sender's `pub_key_b64` from this
   project's `peers.yaml`. Bad sig → 401.
2. **Known sender** — `from_court` must appear in this project's
   `peers.yaml`. Unknown → 403.
3. **Role whitelist** — the `to:` role must be listed in
   `federation.expose_roles`. Default is `[foreman]`, so only the
   foreman is reachable from outside; foreman then routes work
   internally. Off-list → 403.
4. **Policy engine** (PR-2) — see next section.

PR-1 ships the network + identity + role whitelist; PR-2 adds the
policy layer below; later PRs add LLM judge, human approval over
FeiShu/WeChat, and IM redundancy.

### Policy engine (PR-2)

After signature + role checks pass, every inbound message is graded
by the policy engine and routed to one of four outcomes:

| Decision | Goes to | When |
|---|---|---|
| `auto_pass` | `bus/<peer>/inbox/` | tier_c peer, clean body, paths within allow list — *or* PR-3 LLM judge said so on a tier_b message |
| `human_required` | `bus/<peer>/pending-approval/` | tier_a peer, sensitive keyword, attach outside allow_paths, *or* PR-3 LLM judge upgraded a tier_b message |
| `denied` | `bus/<peer>/denied/` *(audit only)* | attach matches a deny path. Never reaches foreman. |

PR-3 wired an actual LLM in for tier_b. When policy says `judge`, the
daemon calls the configured LLM CLI (`default_cli` by default, e.g.
Claude Code) with a built-in judge system prompt, parses a JSON
verdict, and applies a confidence threshold. Any failure (CLI not on
PATH, timeout, unparseable output, low confidence) **fails safe to
`human_required`** — the receiver is never worse off than they would
have been without PR-3.

Configuration lives in two files per project:

- **`court.yaml`** —
  - `federation.allow_paths` / `deny_paths` — path globs that
    constrain what files an inbound message may reference via its
    `attaches:` frontmatter field.
  - `federation.judge` — which CLI to invoke for tier_b judgement,
    optional `model`, optional `prompt_file` override,
    `timeout_seconds` (default 30), `confidence_threshold`
    (default 0.6). Falls back to top-level `default_cli` when
    `judge.cli` is unset.
- **`policy.yaml`** — `default_tier:` (one of `tier_a`/`tier_b`/`tier_c`)
  + optional `sensitive_keywords:` list appended to the built-in one.

`peers.yaml` may pin `policy_tier:` per peer; if absent, falls through
to `policy.yaml`'s `default_tier`.

**Hardcoded layer (cannot be overridden by config).** Paths matching
`**/.ssh/**`, `**/.env`, `**/id_rsa*`, `/etc/**`, `**/credentials.json`,
`**/secrets/**`, `**/.aws/**`, `**/.kube/config` are *always* denied.
Bodies containing `api_key`, `password`, `secret`, `token`, `sk-`,
`AKIA`, etc. always force `human_required`.

Every decision is appended to
`$COURT_ROOT/projects/<p>/logs/policy-log.jsonl` for audit.

See [docs/lan-deployment.en.md](./docs/lan-deployment.en.md) for a full
example with the `attaches:` field.

### Temporary grants (PR-4)

When `allow_paths` is too narrow for a one-off ("Bob, look at
`notes/q2-plan.md` real quick"), the receiver can mint a
time-bounded, peer-scoped grant — a sudo moment, not a config change.

Two grant types, distinguished by `grant_type`:

| Type | Widens... | Use when |
|---|---|---|
| `path` (default) | `allow_paths` | one-off attach outside the configured whitelist |
| `tier` | the peer's `policy_tier` for the soft layer | want to wave through a single tier_a/b message without editing peers.yaml |

Hardcoded denies, user `deny_paths`, and `HARDCODED_KEYWORDS` always
still win. Grants can only *add* capabilities, never subtract.

```bash
# Path grant — 30 min for Bob to attach anything under notes/
court-grant example bob "notes/**"
court-grant example bob "shared/draft-*.md" --ttl 2h

# Tier grant — upgrade Bob to tier_c for one message only
court-grant example bob --tier tier_c --once

# Tier grant — upgrade for an hour
court-grant example bob --tier tier_c --ttl 1h

court-grant example list
# STATE     T ID         PEER  EXPIRES                  HITS DETAIL
# active    P 4616c19a   bob   2026-05-13T22:53:00+...  0    notes/**
# active    T 7fa20bd8   bob   2026-05-13T23:00:00+...  0    →tier_c [once]

court-grant example info 4616c19a       # full record + remaining time + hit count
court-grant example revoke 4616c19a
```

The `T` column is `P` for path grants, `T` for tier grants. `info`
shows `state`, `remaining`, `hit_count`, `last_hit_ts`, and
(for once-grants) `consumed_ts`.

Grants are JSON files under `$COURT_ROOT/projects/<p>/grants/`,
written atomically and validated on read (oversize / malformed
files are skipped with a warning to `logs/peer-errors.log`).
Durable across daemon restarts; `revoke` deletes the file. From an
upstream LLM the same surface is exposed as `grant_peer_access` /
`grant_peer_tier` / `grant_info` / `list_grants` / `revoke_grant`.

The `project` argument on every grant entry point is validated for
filesystem-component safety AND containment under
`$COURT_ROOT/projects/`. Passing `project="../foo"` returns an error
rather than reading from outside the projects root.

For a full two-machine walk-through see [docs/lan-deployment.en.md](./docs/lan-deployment.en.md).

## Directory layout

```
$COURT_ROOT/                                  # default ~/.agent-court
├── projects/
│   └── myproject/
│       ├── court.yaml                        # project config (+ federation block)
│       ├── peers.yaml                        # this project's known peers
│       ├── policy.yaml                       # PR-2: tier + sensitive keywords (optional)
│       ├── identity/                         # this project's keypair (mode 0600/0644)
│       │   ├── priv.key
│       │   └── pub.key
│       ├── grants/                           # PR-4: one JSON file per active/expired grant
│       │   └── <id>.json
│       ├── prompts/
│       │   ├── foreman.md
│       │   ├── frontend.md
│       │   └── ...                           # one per role
│       ├── bus/
│       │   ├── foreman/{inbox,outbox,inbox/.done}/
│       │   ├── frontend/...
│       │   ├── backend/...
│       │   ├── upstream/...                  # MCP caller's outbox/inbox
│       │   ├── human/...                     # your CLI sends land here
│       │   └── <peer_court_id>/              # inbound peer messages, fanned by decision
│       │       ├── inbox/                    #   auto_pass + judge land here
│       │       ├── pending-approval/         #   human_required parks here
│       │       └── denied/                   #   denied (audit only, never delivered)
│       ├── shared/event.log
│       └── logs/{watcher.log, peer-errors.log, policy-log.jsonl, watcher.pid}
```

The repository itself (this one) only ships:
- `bin/` — shell scripts (`court-up`, `court-down`, `court-watcher`,
  `court-send`, `role-launch`, `court-keygen`, `court-peer`, `court-grant`)
- `mcp/court-mcp/` — the Python MCP server + peer daemon + keygen
- `projects/example/` — a fork-me example project (with a commented-out
  `federation:` block as schema reference)
- `docs/` — extra docs (LAN deployment, cc-connect bridge, etc.)

Your actual courts live under `$COURT_ROOT` (default `~/.agent-court/`),
*outside* the repo.

## FAQ

### Why not just use sub-agents / a single agent framework?

Sub-agents (in Claude Code, AutoGen, CrewAI, etc.) decide *for you* when to
spawn workers, hide them in background context, and tear them down on
completion. You can't `tmux attach` and watch an agent think, you can't fork
its system prompt mid-task, and the message graph is owned by the framework.

`agent-court` is closer to a tiny operating system: long-running role
processes, filesystem IPC, an external watcher. Worse abstraction, better
inspectability.

### Is it just for coding?

No. Roles are free-form. Anything you can describe in a system prompt is a
valid role: a researcher who pulls trends, a copywriter who turns research
into scripts, an analyst who reads logs. Pair it with whatever LLM CLI you
want.

### How does this interact with my existing LLM CLI?

`role-launch` invokes your CLI with `--append-system-prompt <prompt file>`
and (optionally) `--model <model>`. If your CLI uses a different flag, set
the role's `cli` field in `court.yaml` to a small wrapper script.

### Why is each project a separate keypair? Can't I share one across projects?

You *could*, but the design specifically discourages it. The point of
per-project keys is that "Alice's work for client A" and "Alice's work
for client B" are two different courts on the network — a peer Alice
federated with for project A cannot, by virtue of that trust, also see
or dispatch into project B. Different projects = different
`court_id`s + different keys + different `peers.yaml`. Re-using a
keypair across projects would collapse that isolation.

### What about cost?

Each role is an independent CLI session, so context isn't shared — every
role re-reads its own system prompt + bus inbox. That's the trade-off for
isolation. If you only have one project active, run only that project.

## License

MIT. See [LICENSE](./LICENSE).

## Status

Early. PR-1 (HTTP + identity + signed dispatch + role whitelist),
PR-2 (policy engine + path-level allow/deny + sensitive-keyword
filter + pending-approval bin), PR-3 (LLM judge for the `tier_b`
branch, with fail-safe fallback to human_required), and PR-4
(sudo-style temporary grants — peer-scoped, time-bounded grants
that widen `allow_paths` (path grants) or override the soft tier
(tier grants, with optional `--once` semantics), via `court-grant`
+ MCP; hardened against path traversal, atomic writes, strict
JSON validation) are working with 150+ tests. PR-5 (multi-channel
human approval: terminal + FeiShu + WeChat) and PR-6 (IM
redundancy) are next. Bug reports
and prompts for new role archetypes welcome — open an issue.
