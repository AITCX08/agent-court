[中文](./lan-deployment.md) | **English**

# LAN deployment — two-machine quickstart

Walk-through for getting two `agent-court` projects talking on the same
local network. No public IPs, no VPN — works the moment both machines
can ping each other on the LAN.

> Status: PR-1 (HTTP + signing + role whitelist), PR-2 (policy
> engine + path/keyword gating + pending-approval bin), PR-3 (LLM
> judge with fail-safe fallback), and PR-4 (sudo-style temporary
> path grants via `court-grant`) are live. Still ahead: PR-5
> multi-channel human approval (FeiShu/WeChat), PR-6 IM redundancy,
> and TLS.

## Mental model first

A "court" lives at **one project on one machine** —
`$COURT_ROOT/projects/<project>/`. Each project has its own keypair,
its own `peers.yaml`, and its own `court_id`. Two projects on the same
machine cannot infer each other's existence; they are separate courts
to the outside world.

That means **everything below must be repeated for each project pair
you want federated**. Generating a keypair once for `project-A` does
*not* let `project-B` on the same machine talk to anything.

## Pre-flight

On each machine:

1. `agent-court` checked out, `bin/` on PATH.
2. The MCP server venv installed:
   ```bash
   cd /path/to/agent-court/mcp/court-mcp
   uv venv .venv
   uv pip install --python .venv/bin/python -e .
   ```
3. The project you want to federate exists under
   `$COURT_ROOT/projects/<project>/`. The shipped example works:
   ```bash
   cp -r projects/example ~/.agent-court/projects/example
   ```

## 1. Generate the project's keypair

On **Alice's** machine, for the `example` project:

```
$ court-keygen example
[court-keygen] new keypair for project 'example':
  /Users/alice/.agent-court/projects/example/identity/priv.key  (mode 0600)
  /Users/alice/.agent-court/projects/example/identity/pub.key   (mode 0644)

public key      : MCowBQYDK2VwAyEAaG6...     # base64 ed25519 pubkey
fingerprint     : 7a4c0b9e3d2f8a16          # SHA-256 prefix, 16 hex chars

Share both with the peer who will federate with this project.
They paste them into THEIR project's peers.yaml under the entry for you.
```

On **Bob's** machine: same, for *his* `example` project. Each side now
has a per-project `priv.key` / `pub.key` under that project's
`identity/` directory.

Re-running `court-keygen example` is a no-op unless you pass `--force`.

## 2. Enable federation in `court.yaml`

By default the `federation:` block in `court.yaml` is commented out —
the daemon refuses to start. Uncomment it (or write your own) and
configure the whitelist:

```yaml
# ~/.agent-court/projects/example/court.yaml
federation:
  enabled: true

  # Your court_id on the network. Defaults to "<hostname>-<project>".
  court_id: "alice-laptop-example"

  # Which roles outside peers may dispatch *to*. Default: only foreman.
  expose_roles:
    - foreman

  # PR-2: paths the policy engine checks any inbound `attaches:` field
  # against. Allow non-empty + attach not covered → human_required.
  # Deny match (here OR in the hardcoded list) → denied.
  allow_paths:
    - "bus/foreman/inbox/**"
    - "shared/notes-public.md"
  deny_paths:
    - "prompts/**"
    - "shared/notes-private.md"
```

The `expose_roles` whitelist and `allow_paths` / `deny_paths` are
both enforced. After the role check passes, the policy engine grades
the message and routes it to inbox / pending-approval / denied — see
the "Policy gating" section below.

## 3. Exchange fingerprints + public keys

Out of band (Signal, in-person, etc.), share for each project:

| Field | Value to send |
|---|---|
| `court_id` | What the other side will reference you as. Defaults to `<hostname>-<project>`; override under `federation.court_id` in `court.yaml`. |
| `fingerprint` | The 16-byte hex `court-keygen` printed. Lets the other side eyeball-verify the key on first paste. |
| `pub_key_b64` | The full base64 public key (also from `court-keygen` output, or `cat $COURT_ROOT/projects/<project>/identity/pub.key`). **Required at runtime** — without it the peer cannot verify your signatures. |

## 4. Write `peers.yaml` on each side

This file lives **inside the project**, not in a shared config dir.

`~/.agent-court/projects/example/peers.yaml` on Alice:

```yaml
self:
  court_id: "alice-laptop-example"
  pub_key_fingerprint: "7a4c0b9e3d2f8a16"     # informational

peers:
  - name: "Bob"
    court_id: "bob-laptop-example"
    url: "http://192.168.1.50:8765"
    pub_key_fingerprint: "f0e1d2c3b4a59687"
    pub_key_b64: "MCowBQYDK2VwAyEAhV0z..."     # Bob's public key
    relation: "sibling"                        # parent | child | sibling
```

`~/.agent-court/projects/example/peers.yaml` on Bob — symmetric, listing
Alice (with `relation: sibling` on his side too).

Replace IPs with your own. `ip addr` on Linux or `ipconfig getifaddr en0`
on macOS to find your LAN address.

> The `relation:` field replaces the legacy `role:` field. The loader
> still accepts `role:` for backward compatibility, but write new
> configs with `relation:`. It's informational in PR-1; PR-2 will use
> it to let policy rules vary by relation (e.g. a parent court's
> dispatches are auto-allowed without approval).

## 5. Start the receiver daemon

On each side, **per project** you want to receive:

```bash
court-peer example
```

This binds `0.0.0.0:8765` and serves `POST /inbox` + `GET /healthz`.
Override the bind with `--bind` or `COURT_PEER_BIND`:

```bash
COURT_PEER_BIND=192.168.1.50:9000 court-peer example
```

If you federate multiple projects on the same machine, give each its
own port:

```bash
COURT_PEER_BIND=0.0.0.0:8765 nohup court-peer example   > ~/.agent-court/logs/peer-example.log 2>&1 &
COURT_PEER_BIND=0.0.0.0:8766 nohup court-peer client-a  > ~/.agent-court/logs/peer-client-a.log 2>&1 &
COURT_PEER_BIND=0.0.0.0:8767 nohup court-peer ops       > ~/.agent-court/logs/peer-ops.log 2>&1 &
```

Each project gets a different peer URL (e.g. `http://host:8765` vs
`http://host:8766`); the remote side puts whichever URL they need
into their `peers.yaml` entry for that project.

Identities, peers, policies, and bus directories are all
project-scoped — three daemons on the same host effectively run
three independent courts, and a remote peer authorized to dispatch
into `example` cannot in any way reach `client-a` or `ops`.

If federation is disabled in that project's `court.yaml`, the daemon
refuses to start with a pointer to the config block.

## 6. Send a test message from Alice → Bob

From any MCP-aware client connected to Alice's `court-mcp` server
(Claude Code, Cursor, Zed, a custom assistant), call:

```python
list_peers(project="example")
# returns: {project, self: {court_id, fingerprint, federation_enabled, ...},
#           peers: [...]}.  reachable=true once Bob's daemon is up.

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="hi from Alice — please look at issue #42",
    target_role="foreman",
)
# returns: {
#   http_status: 200,
#   response: {
#     status: "accepted",
#     file_path: ".../bus/alice-laptop-example/inbox/<file>.md",
#     id: ...
#   }
# }
```

On Bob's machine the file shows up at:

```
~/.agent-court/projects/example/bus/alice-laptop-example/inbox/1715432400-7f3d2e1a-upstream-to-foreman.md
```

Bob still has to surface that file to his foreman manually for now —
the `court-watcher` daemon only listens on the local roles'
`*/outbox/` directories, so peer-inbox files sit there until someone
reads them. The supported workflow today:

```bash
# On the receiver, periodically:
ls ~/.agent-court/projects/example/bus/*/inbox/*.md
# Promote any file you want delivered to the foreman:
mv .../bus/<peer-court-id>/inbox/<file>.md \
   .../bus/foreman/inbox/<file>.md
```

A future PR will teach `court-watcher` to also auto-route peer-inbox
files into the target role's inbox once the policy decision says
`auto_pass`.

## Policy gating (PR-2)

Once an inbound message clears signature + role checks, the policy
engine grades it and routes it to one of three on-disk locations:

| Decision | Lands at | Means |
|---|---|---|
| `auto_pass` / `judge` | `bus/<peer>/inbox/` | Delivered to foreman normally |
| `human_required` | `bus/<peer>/pending-approval/` | Waiting — a human must `mv` it into inbox |
| `denied` | `bus/<peer>/denied/` | Audit-only; never reaches foreman |

The response from `dispatch_to_peer` always shows the decision so the
sender's LLM can react:

```json
{
  "http_status": 200,
  "response": {
    "status": "pending_approval",
    "decision": "human_required",
    "tier": "hard_rule",
    "reasons": ["sensitive keyword 'password' in body → human_required"],
    "file_path": ".../bus/alice-laptop-example/pending-approval/...md"
  }
}
```

### Optional: `policy.yaml`

Add `~/.agent-court/projects/example/policy.yaml` to tune the default
tier and add custom sensitive keywords:

```yaml
default_tier: tier_b           # tier_a (human) | tier_b (judge) | tier_c (auto)
sensitive_keywords:
  - "wire transfer"
  - "merger"
```

If the file is missing the defaults are `tier_b` + no extra keywords.

### Optional: LLM judge (PR-3)

When a message hits the `tier_b → judge` slot the daemon invokes an
LLM CLI to decide between `auto_pass` and `human_required`. With no
configuration the daemon uses `claude` (or whatever `default_cli` in
`court.yaml` is) and a built-in prompt at
`mcp/court-mcp/prompts/judge.md`.

```yaml
# ~/.agent-court/projects/example/court.yaml
default_cli: claude               # also used by the LLM judge

federation:
  enabled: true
  judge:
    # cli: claude                 # override default_cli for the judge only
    # model: haiku                # --model flag (passes through to the CLI)
    # prompt_file: /etc/agent-court/strict-judge.md
    timeout_seconds: 30
    confidence_threshold: 0.6
```

The judge's prompt asks for strict JSON:

```
{"verdict": "auto_pass" | "human_required", "confidence": 0.0-1.0, "reason": "..."}
```

Anything that goes wrong (CLI missing, timeout, unparseable output,
confidence below `confidence_threshold`) **fails safe** to
`human_required`. The exact failure mode is preserved in the
`policy-log.jsonl` `reasons` array — tail it after suspicious
deliveries.

Use a custom `prompt_file` to teach the judge about your project's
specific risk surface (e.g., "treat any reference to billing
endpoints as human_required"). The built-in prompt is intentionally
generic.

### Temporary grants (PR-4)

When the *receiver* wants to let a specific peer poke at a file
outside `allow_paths` for a short while ("just look at
`notes/q2-plan.md` for the next 30 minutes"), or wave a single
message past the soft-tier review, they mint a grant instead of
editing `court.yaml`. Grants are time-bounded, peer-scoped, and
only ever *add* capabilities — hardcoded denies (`.ssh`, `.env`,
`/etc`, `credentials.json`, etc.) and the user's own `deny_paths`
always still win.

Two grant types:

| Type | What it relaxes | Use |
|---|---|---|
| **path** | `allow_paths` | the attach you want through isn't in the static whitelist |
| **tier** | the peer's `policy_tier` for one (`--once`) or many messages | want to skip judge/human review for a known-good batch |

```bash
# Path grants — common case: `add` is implicit.
court-grant example bob "notes/**"
court-grant example bob "shared/draft-*.md" --ttl 2h

# Tier grants — pass --tier <tier>. Add --once for fire-once semantics.
court-grant example bob --tier tier_c --once          # one free pass
court-grant example bob --tier tier_c --ttl 1h        # window of trust

# List active + expired grants for a project.
court-grant example list
# STATE     T ID         PEER  EXPIRES                       HITS DETAIL
# active    P 4616c19a   bob   2026-05-13T22:53:00+08:00     0    notes/**
# active    T 7fa20bd8   bob   2026-05-13T23:00:00+08:00     0    →tier_c [once]
# consumed  T 9a01ee3c   bob   2026-05-13T22:00:00+08:00     1    →tier_c [once]

# Inspect one grant in detail.
court-grant example info 4616c19a
# id            : 4616c19a
# grant_type    : path
# state         : active
# granted_to    : bob
# paths         : ['notes/**']
# issued_ts     : 2026-05-13T22:23:00+08:00
# issued_by     : alice@laptop
# expires_ts    : 2026-05-13T22:53:00+08:00
# remaining     : 27m13s
# hit_count     : 2
# last_hit_ts   : 2026-05-13T22:35:18+08:00
# file          : /Users/alice/.agent-court/projects/example/grants/4616c19a.json

# Kill a grant before its TTL.
court-grant example revoke 4616c19a
```

Each grant is a JSON file at
`$COURT_ROOT/projects/<p>/grants/<id>.json`, written atomically
(`tempfile + os.replace`) so a daemon reading the directory never
sees a half-written record. Durable across restarts — no
in-memory state to lose. The daemon re-reads `grants/` on every
inbound request, so a fresh `mint` / `revoke` / consumption takes
effect on the next message with **no daemon reload**.

Field reference (the on-disk JSON shape):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | 8 hex chars; doubles as filename. |
| `grant_type` | `"path"` \| `"tier"` | Which knob this grant turns. |
| `granted_to` | string | Peer `court_id`. Must match `from_court` on inbound. |
| `paths` | list[string] | (path grant) globs OR'd into `allow_paths`. |
| `target_tier` | string | (tier grant) `tier_a` / `tier_b` / `tier_c`. |
| `consume_on_use` | bool | (tier grant) if true, marks consumed after first hit. |
| `consumed_ts` | string \| null | When the once-grant fired. Null until then. |
| `issued_ts` / `expires_ts` | string (ISO 8601) | TTL boundaries. |
| `issued_by` | string ≤ 128 | Free-form audit tag (`$USER@$HOST`). |
| `hit_count` | int | How many inbound messages have matched this grant. |
| `last_hit_ts` | string \| null | Timestamp of the most recent match. |

The same surface is exposed via MCP for upstream LLMs that have
been delegated this authority:

```python
grant_peer_access(
    project="example",
    peer_court_id="bob-laptop-example",
    paths=["notes/**"],
    ttl="1h",
)
# → {project, id, grant_type: "path", granted_to, paths, ...,
#    hit_count: 0, remaining_seconds: 3600}

grant_peer_tier(
    project="example",
    peer_court_id="bob-laptop-example",
    target_tier="tier_c",
    consume_on_use=True,
)
# → {project, id, grant_type: "tier", target_tier, consume_on_use, ...}

list_grants(project="example")
# → {project, active: [...], expired: [...]} (each entry includes
#   grant_type, hit_count, remaining_seconds)

grant_info(project="example", grant_id="4616c19a")
# → {state: "active"|"expired", ...full record...}

revoke_grant(project="example", grant_id="4616c19a")
# → {ok: true, result: "revoked", grant_id}
# Errors: invalid_id | not_found | io_error
```

#### Safety properties (worth knowing before you delegate the MCP tool)

- **Path containment.** Every grant entry point validates `project`
  as a safe filesystem component AND verifies the resolved
  directory lives strictly inside `$COURT_ROOT/projects/`. A caller
  supplying `project="../foo"` gets an error instead of arbitrary
  filesystem access.
- **TTL cap.** `parse_ttl` rejects values past 1 year so datetime
  arithmetic can never overflow; MCP/CLI surface that as a clean
  `invalid_argument` error.
- **Strict JSON schema.** Grant files are parsed strictly on read;
  missing fields, wrong types, or oversized payloads
  (> 64 KB) are skipped with a warning to `logs/peer-errors.log`
  rather than silently honored.
- **Atomic writes.** Mint / record_hit / mark_consumed all use
  same-directory tempfile + `os.replace`, so a reader iterating
  `glob("*.json")` either sees the old content or the new content,
  never a torn write.
- **Peer existence check (MCP).** `grant_peer_access` /
  `grant_peer_tier` decline to mint when `peers.yaml` is present
  and the named `peer_court_id` isn't in it (prevents typo-driven
  "orphan" grants that would silently activate if the peer ever
  joined later). The CLI is loose by design — for bootstrap-time
  use before `peers.yaml` is wired up.

When an inbound `attaches:` path is covered by an active grant
(and not by `allow_paths` already), the decision's `reasons`
will explicitly call it out — useful audit signal:

```json
{
  "decision": "auto_pass",
  "reasons": ["attach 'notes/q2-plan.md' covered by active grant pattern 'notes/**'"]
}
```

Grants survive daemon restarts. Expiry is enforced at read time
(`is_active(now)`), so a wall-clock change can't accidentally
revive an expired grant.

### Per-peer tier (in `peers.yaml`)

```yaml
peers:
  - name: "External vendor"
    court_id: "vendor-build-bot"
    relation: "sibling"
    policy_tier: "tier_a"          # untrusted: everything → pending-approval
```

### Trying it: `attaches` + `dispatch_to_peer`

```python
dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="please review the diff",
    attaches=["bus/foreman/inbox/diff.md"],   # passes allow_paths
)
# → decision: judge (or auto_pass if tier_c)

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="here is the prod password=hunter2",
)
# → decision: human_required (keyword)

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="have a look",
    attaches=["~/.ssh/id_ed25519"],
)
# → decision: denied (hardcoded path)
```

The decision trail is appended to
`~/.agent-court/projects/example/logs/policy-log.jsonl`:

```bash
tail -f ~/.agent-court/projects/example/logs/policy-log.jsonl
```

### Approving a `pending-approval` message

There is no approval UI yet (PR-5 adds terminal + FeiShu + WeChat).
For now, eyeball the file and move it manually:

```bash
cd ~/.agent-court/projects/example/bus/alice-laptop-example
cat pending-approval/*.md           # read the body + policy_reasons
mv pending-approval/<file>.md inbox/   # release to foreman
```

## Firewall checklist

`court-peer` is plain HTTP, no TLS. Open the port both ways on each
machine's firewall — most home LANs are wide open already.

| OS | Allow inbound TCP 8765 |
|---|---|
| macOS | `System Settings → Network → Firewall → Options → Add court-peer's python binary "Allow"` |
| Ubuntu | `sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp` |
| Windows | `New-NetFirewallRule -DisplayName "agent-court" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow` |

## Troubleshooting

### "transport_error" in `dispatch_to_peer` response

- Verify the URL is reachable: `curl http://192.168.1.50:8765/healthz` from Alice.
- If `curl` hangs → firewall is dropping. See firewall checklist.
- If `Connection refused` → daemon isn't running on that IP/port. Check
  `ps aux | grep peer_daemon`.

### 401 `bad_signature` or `missing_peer_pub_key`

- The peer rejected your signature. Almost always one of:
  - `pub_key_b64` for your `court_id` in *their* project's
    `peers.yaml` doesn't match your current `priv.key`. Did you
    regenerate `court-keygen`? You must re-share the new public key.
  - You and the peer disagree on which fields go into the signed
    payload. Both sides must be on the same `agent-court` version.
  - You pointed `dispatch_to_peer` at the wrong `project=...`, so the
    private key being used to sign belongs to a different court than
    the peer expects.
- Check `$COURT_ROOT/projects/<project>/logs/peer-errors.log` on the
  receiving side for the specific failure reason.

### 403 `federation_disabled`

- The peer's `court.yaml` has no `federation:` block, or
  `federation.enabled: false`. The flag is re-read per-request, so the
  peer can flip it back to `true` without restarting the daemon.

### 403 `unknown_sender`

- Your `court_id` isn't in the peer's `peers.yaml`. Ask them to add
  you, or check that the `court_id` you're using matches what they
  configured. Remember: each project has its own `peers.yaml` — being
  listed in their `project-A/peers.yaml` does not grant access to
  `project-B`.

### 403 `role_not_exposed`

- You dispatched to a role not in the peer's `federation.expose_roles`
  list. By default only `foreman` is exposed; ask the peer to either
  route via foreman or add your target role to `expose_roles`.

### `decision: denied` in response

- An attach matched a deny rule (yours or hardcoded). The message is
  *not* delivered — it sits in `bus/<your-court-id>/denied/` on the
  receiver for audit. Inspect the `reasons` field in the response:
  ```
  "reasons": ["attach '/etc/passwd' hits hardcoded deny '/etc/**'"]
  ```
- Hardcoded denies cannot be lifted from `court.yaml` *or* by a
  PR-4 grant — by design. If you genuinely need that path,
  restructure the dispatch (e.g. paste the relevant content into
  the body). For paths only blocked by your own
  `allow_paths`/`deny_paths`, ask the peer to mint a temporary
  grant: `court-grant <project> <your-court-id> "<glob>"` (see
  "Temporary grants" above).

### `decision: human_required` / `status: pending_approval`

- Either the sender's peer entry is `policy_tier: tier_a`, or the
  body triggered a sensitive-keyword match, or an attach landed
  outside `allow_paths`, or the PR-3 LLM judge upgraded an
  otherwise-passing tier_b message. The file is in
  `bus/<your-court-id>/pending-approval/` on the receiver; a human
  there must `mv` it to `inbox/` to actually deliver.
- Check the receiver's `logs/policy-log.jsonl` — every decision has a
  `reasons` array explaining which rule fired.

### `tier: llm_judge_failed` showing up in logs

- The PR-3 judge tried to call an LLM CLI and something went wrong.
  Look at the message's `reasons` array — it pins the exact failure:
  - `"cli '<x>' not found on PATH"` → install the CLI on the
    receiver's machine, or point `federation.judge.cli` at one
    that exists.
  - `"<x> timed out after Ns"` → the CLI took too long. Either
    raise `federation.judge.timeout_seconds`, or use a faster
    model via `federation.judge.model`.
  - `"<x> exited <n>: ..."` → the CLI errored out (often a quota
    or auth issue). Run the same command by hand to reproduce.
  - `"no JSON object found in LLM output"` / `"verdict must be
    ..."` → the model drifted off the JSON contract. Tighten the
    prompt or switch model.
- All of these collapse to `human_required` — so a misconfigured
  judge never *delivers* messages; it just over-flags them. Fix at
  your leisure.

### `list_peers` shows `reachable: false`

- Other side's daemon down or unreachable. Same as the transport_error
  checks above.

## Going beyond LAN

For machines on different networks:

- **Recommended**: install [tailscale](https://tailscale.com) on both
  machines, use the tailscale-assigned IP in `peers.yaml`. Same as LAN
  from then on, plus end-to-end encryption.
- **Self-hosted**: run `frp` or `cloudflared` to expose your court-peer
  port. Put the public URL in `peers.yaml`. Pair it with a real TLS
  reverse proxy if the traffic crosses the open internet (PR-1 doesn't
  ship TLS).

Both will be documented under `docs/networking.md` in a follow-up PR.
