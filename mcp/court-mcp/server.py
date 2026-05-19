#!/usr/bin/env python
"""agent-court MCP server.

Exposes an agent-court's filesystem message bus as MCP tools so that an
*upstream* LLM client (Claude Code, Cursor, Zed, a custom assistant, etc.)
can dispatch work down into one of the running courts.

Tools:
- ``list_projects()`` -- enumerate available courts and their roles.
- ``dispatch_to_foreman(project, message, ...)`` -- write a markdown
  message into ``bus/upstream/outbox/`` for routing to the project's
  foreman (or any other role).
- ``query_court_status(project, tail_lines=30)`` -- summarise event.log
  tail and per-role inbox depth + watcher health.
- ``read_upstream_inbox(project, mark_done=False)`` -- collect replies
  the project's foreman has written back to the upstream caller.
- ``list_peers(project=None)`` -- list known peer courts from
  ``peers.yaml`` and probe reachability.
- ``dispatch_to_peer(peer_court_id, project, message, ...)`` -- sign and
  POST a message into a remote court's ``/inbox`` endpoint.

Environment:
- ``COURT_ROOT`` overrides the default home dir (``~/.agent-court``).
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from mcp.server.fastmcp import FastMCP

import lingpai
import bangjiao
from gitea_client import GiteaClient, GiteaClientError
from gitea_credentials import CredentialNotFoundError


COURT_ROOT = Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court")))
PROJECTS_DIR = COURT_ROOT / "projects"
UPSTREAM_ROLE = "upstream"


def _project_dir(project: str) -> Path:
    p = PROJECTS_DIR / project
    if not p.is_dir():
        available = (
            [d.name for d in PROJECTS_DIR.iterdir() if d.is_dir()]
            if PROJECTS_DIR.exists()
            else []
        )
        raise ValueError(f"project '{project}' not found. available: {available}")
    return p


def _load_config(project: str) -> dict:
    cfg = _project_dir(project) / "court.yaml"
    if not cfg.is_file():
        raise ValueError(f"missing {cfg}")
    with cfg.open() as f:
        return yaml.safe_load(f)


def _ensure_role_dirs(project_root: Path, role: str) -> None:
    for sub in ("inbox", "outbox", "inbox/.done"):
        (project_root / "bus" / role / sub).mkdir(parents=True, exist_ok=True)


def _gen_id() -> str:
    return secrets.token_hex(4)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


mcp = FastMCP("agent-court")


@mcp.tool()
def list_projects() -> dict:
    """List projects under ``$COURT_ROOT/projects/`` with their roles and tmux session names."""
    if not PROJECTS_DIR.exists():
        return {"projects": []}
    out = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        cfg_path = d / "court.yaml"
        if not cfg_path.is_file():
            continue
        try:
            with cfg_path.open() as f:
                cfg = yaml.safe_load(f)
            out.append({
                "project": d.name,
                "session": cfg.get("session"),
                "attach_window": cfg.get("attach_window"),
                "roles": [r.get("name") for r in cfg.get("roles", [])],
                "utility_windows": [w.get("name") for w in cfg.get("utility_windows", [])],
            })
        except Exception as e:  # pragma: no cover - defensive
            out.append({"project": d.name, "error": str(e)})
    return {"projects": out}


@mcp.tool()
def dispatch_to_foreman(
    project: str,
    message: str,
    reply_to: Optional[str] = None,
    target_role: str = "foreman",
) -> dict:
    """Dispatch a message into a project's bus from the upstream caller.

    Args:
        project: project name (see ``list_projects()``).
        message: markdown body of the message.
        reply_to: optional id of a previous message this one replies to.
        target_role: defaults to ``foreman``. May be any role registered in
            the project's ``court.yaml`` if you want to skip the foreman.

    The message is written to ``bus/upstream/outbox/`` so the watcher routes
    it to ``bus/<target_role>/inbox/``. The watcher must be running for the
    message to actually arrive; if it isn't, the file sits in the outbox
    until it is.
    """
    proj_root = _project_dir(project)
    cfg = _load_config(project)
    valid_roles = {r["name"] for r in cfg.get("roles", [])}
    if target_role not in valid_roles:
        raise ValueError(
            f"target_role '{target_role}' not in {sorted(valid_roles)} "
            f"for project '{project}'"
        )

    _ensure_role_dirs(proj_root, UPSTREAM_ROLE)

    msg_id = _gen_id()
    ts = _iso_now()
    ts_epoch = int(datetime.now().timestamp())
    fname = f"{ts_epoch}-{msg_id}-{UPSTREAM_ROLE}-to-{target_role}.md"
    fpath = proj_root / "bus" / UPSTREAM_ROLE / "outbox" / fname

    lines = [
        "---",
        f"from: {UPSTREAM_ROLE}",
        f"to: {target_role}",
        f"ts: {ts}",
        f"id: {msg_id}",
    ]
    if reply_to:
        lines.append(f"in_reply_to: {reply_to}")
    lines.append("---")
    lines.append("")
    lines.append(message)
    fpath.write_text("\n".join(lines) + "\n")

    return {
        "file_path": str(fpath),
        "id": msg_id,
        "from": UPSTREAM_ROLE,
        "to": target_role,
        "project": project,
        "ts": ts,
        "note": (
            "watcher routes outbox -> bus/<to>/inbox/. "
            "If `court-up <project>` is not running, the message sits in outbox."
        ),
    }


@mcp.tool()
def query_court_status(project: str, tail_lines: int = 30) -> dict:
    """Snapshot a project's court: event.log tail + per-role inbox depth + watcher liveness."""
    proj_root = _project_dir(project)
    cfg = _load_config(project)
    session = cfg.get("session")

    event_log = proj_root / "shared" / "event.log"
    tail = []
    if event_log.is_file():
        with event_log.open() as f:
            lines = f.readlines()
        tail = [ln.rstrip("\n") for ln in lines[-tail_lines:]]

    inbox_counts = {}
    done_counts = {}
    bus_dir = proj_root / "bus"
    if bus_dir.is_dir():
        for role_dir in sorted(bus_dir.iterdir()):
            if not role_dir.is_dir():
                continue
            inbox = role_dir / "inbox"
            done = role_dir / "inbox" / ".done"
            if inbox.is_dir():
                inbox_counts[role_dir.name] = sum(
                    1 for f in inbox.iterdir() if f.is_file() and f.suffix == ".md"
                )
            if done.is_dir():
                done_counts[role_dir.name] = sum(
                    1 for f in done.iterdir() if f.is_file() and f.suffix == ".md"
                )

    watcher_pid_file = proj_root / "logs" / "watcher.pid"
    watcher_alive = False
    watcher_pid = None
    if watcher_pid_file.is_file():
        try:
            watcher_pid = int(watcher_pid_file.read_text().strip())
            os.kill(watcher_pid, 0)
            watcher_alive = True
        except (ValueError, ProcessLookupError, PermissionError):
            watcher_alive = False

    return {
        "project": project,
        "session": session,
        "watcher_alive": watcher_alive,
        "watcher_pid": watcher_pid,
        "inbox_pending": inbox_counts,
        "inbox_done": done_counts,
        "event_log_tail": tail,
        "event_log_path": str(event_log),
    }


@mcp.tool()
def read_upstream_inbox(project: str, mark_done: bool = False) -> dict:
    """Read messages addressed to the upstream caller (replies from foreman, etc.).

    Args:
        project: project name.
        mark_done: if True, move each message to ``inbox/.done/`` after reading.
            Defaults to False so the upstream client controls retention.

    Returns:
        dict with ``count`` and ``messages`` (list of parsed frontmatter + body).
    """
    proj_root = _project_dir(project)
    inbox = proj_root / "bus" / UPSTREAM_ROLE / "inbox"
    if not inbox.is_dir():
        return {"messages": [], "note": f"no upstream inbox at {inbox}"}

    files = sorted(
        (f for f in inbox.iterdir() if f.is_file() and f.suffix == ".md"),
        key=lambda p: p.stat().st_mtime,
    )
    messages = []
    for f in files:
        msg = _parse_message(f)
        msg["file"] = str(f)
        messages.append(msg)
        if mark_done:
            done_dir = inbox / ".done"
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(done_dir / f.name))

    return {"project": project, "count": len(messages), "messages": messages}


def _parse_message(path: Path) -> dict:
    text = path.read_text()
    if not text.startswith("---"):
        return {"body": text}
    lines = text.split("\n")
    front_lines = []
    body_start = None
    in_fm = False
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                body_start = i + 1
                break
        if in_fm:
            front_lines.append(line)
    front = {}
    for fl in front_lines:
        if ":" in fl:
            k, v = fl.split(":", 1)
            front[k.strip()] = v.strip()
    body = "\n".join(lines[body_start:]).strip() if body_start is not None else ""
    return {
        "from": front.get("from"),
        "to": front.get("to"),
        "id": front.get("id"),
        "in_reply_to": front.get("in_reply_to"),
        "ts": front.get("ts"),
        "body": body,
    }


# ---------------------------------------------------------------------------
# Peer-network tools (inter-court federation)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_peers(project: str) -> dict:
    """List peers registered for ``project``'s federation and probe each for reachability.

    Peers are scoped per project (each project has its own ``peers.yaml`` and
    keypair). An upstream LLM caller picks which project's network they want
    to see before this tool returns anything useful.

    Args:
        project: project name under ``$COURT_ROOT/projects/``.

    Returns:
        dict with ``self`` (this project's court_id + fingerprint + federation
        enabled flag), ``peers`` (each peer's reachable status), and the path
        to the loaded peers.yaml.
    """
    if not bangjiao.project_dir(project).is_dir():
        return {
            "error": "unknown_project",
            "project": project,
            "available": bangjiao.all_projects(),
        }

    fed = bangjiao.load_bangjiao(project)
    try:
        peers_cfg = bangjiao.load_peers(project)
    except FileNotFoundError as e:
        return {"error": str(e), "project": project, "peers": []}

    import aiohttp

    async def _probe(url: str) -> bool:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)
            ) as session:
                async with session.get(f"{url}/healthz") as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def _probe_all() -> list[bool]:
        return await asyncio.gather(*[_probe(p.url) for p in peers_cfg.peers])

    reachable = asyncio.run(_probe_all()) if peers_cfg.peers else []

    return {
        "project": project,
        "self": {
            "court_id": peers_cfg.self_court_id,
            "fingerprint": peers_cfg.self_fingerprint,
            "federation_enabled": fed.enabled,
            "expose_roles": fed.expose_roles,
        },
        "peers": [
            {
                "name": p.name,
                "court_id": p.court_id,
                "url": p.url,
                "relation": p.relation,
                "pub_key_fingerprint": p.pub_key_fingerprint,
                "reachable": reach,
            }
            for p, reach in zip(peers_cfg.peers, reachable)
        ],
        "peers_yaml_path": str(bangjiao.project_peers_yaml_path(project)),
    }


@mcp.tool()
def dispatch_to_peer(
    project: str,
    peer_court_id: str,
    message: str,
    target_role: str = "foreman",
    sender_role: str = "upstream",
    reply_to: Optional[str] = None,
    attaches: Optional[list] = None,
) -> dict:
    """Sign and POST a message to a remote court's /inbox endpoint.

    Signed with the local ``project``'s keypair, so the remote side sees
    ``from_court = <this project's court_id>``. The remote must have that
    court_id in its own peers.yaml for the message to land.

    Args:
        project: this side's project — picks which keypair signs the message
            and which peers.yaml is consulted for ``peer_court_id``.
        peer_court_id: the ``court_id`` of the target peer.
        message: markdown body the remote foreman/role will read.
        target_role: the role inside the remote court that should receive
            the message. Defaults to ``foreman``.
        sender_role: the role name on *this* side that the remote sees as
            ``from``. Defaults to ``upstream`` (i.e. "an upstream LLM
            assistant"); change to ``foreman`` etc. as appropriate.
        reply_to: optional id of a previous message this one replies to.
        attaches: optional list of file paths this message references. The
            remote's policy engine (PR-2) inspects these against allow/deny
            globs and may park the message in pending-approval/ or denied/
            instead of inbox/. Paths are signed, so they cannot be altered
            in transit.

    Returns:
        dict with the remote's response or, on failure, an ``error`` field.
        Never raises — designed for an LLM tool-caller to inspect and react.
        On success the ``response`` field carries the policy decision
        (``decision``, ``tier``, ``reasons``) the remote applied.
    """
    if not bangjiao.project_dir(project).is_dir():
        return {
            "error": "unknown_project",
            "project": project,
            "available": bangjiao.all_projects(),
        }

    try:
        identity = bangjiao.load_identity(project)
    except FileNotFoundError as e:
        return {"error": "no_identity", "detail": str(e), "project": project}

    peers_cfg = bangjiao.load_peers(project)
    peer = peers_cfg.by_court_id(peer_court_id)
    if peer is None:
        return {
            "error": "unknown_peer",
            "project": project,
            "peer_court_id": peer_court_id,
            "available": [p.court_id for p in peers_cfg.peers],
        }

    msg_id = secrets.token_hex(4)
    ts = bangjiao.iso_now()
    msg = {
        "from": sender_role,
        "from_court": peers_cfg.self_court_id,
        "to": target_role,
        "body": message,
        "ts": ts,
        "id": msg_id,
    }
    if reply_to:
        msg["in_reply_to"] = reply_to
    # Only include attaches in the signed payload if the caller actually
    # listed any. Passing an empty list would still make it through canonical
    # JSON as `"attaches":[]`, which is fine, but skipping it entirely keeps
    # the wire compatible with PR-1 senders that don't know about the field.
    if attaches:
        msg["attaches"] = list(attaches)

    msg["signature"] = bangjiao.sign_message(msg, identity.priv)

    import aiohttp

    async def _post() -> dict:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10.0)
        ) as session:
            async with session.post(f"{peer.url}/inbox", json=msg) as resp:
                text = await resp.text()
                try:
                    data = _json.loads(text)
                except Exception:
                    data = {"raw": text}
                return {"http_status": resp.status, "response": data}

    try:
        result = asyncio.run(_post())
    except Exception as e:
        return {
            "error": "transport_error",
            "detail": str(e),
            "project": project,
            "peer_court_id": peer_court_id,
            "id": msg_id,
        }

    return {
        "project": project,
        "peer_court_id": peer_court_id,
        "url": peer.url,
        "id": msg_id,
        "signature_status": "signed",
        "http_status": result["http_status"],
        "response": result["response"],
    }


def _grant_to_row(g, *, include_metrics: bool = True) -> dict:
    """Serialize a Grant dataclass to the dict shape used in MCP replies."""
    row = {
        "id": g.id,
        "grant_type": g.grant_type,
        "granted_to": g.granted_to,
        "paths": g.paths,
        "target_tier": g.target_tier,
        "consume_on_use": g.consume_on_use,
        "consumed_ts": g.consumed_ts,
        "issued_ts": g.issued_ts,
        "expires_ts": g.expires_ts,
        "issued_by": g.issued_by,
    }
    if include_metrics:
        row["hit_count"] = g.hit_count
        row["last_hit_ts"] = g.last_hit_ts
        row["remaining_seconds"] = g.remaining_seconds()
    return row


def _bad_project_reply(project: str) -> dict:
    return {
        "error": "unknown_project",
        "project": project,
        "available": bangjiao.all_projects(),
    }


def _check_peer_exists(project: str, peer_court_id: str) -> Optional[dict]:
    """Return an MCP error dict if ``peer_court_id`` isn't in peers.yaml.

    Returns None when the peer exists (so the caller can proceed) or
    when peers.yaml is missing/empty (loose mode — let mint proceed so
    bootstrap-time grants still work).
    """
    try:
        peers = bangjiao.load_peers(project)
    except Exception:  # noqa: BLE001  — refuse to fail mint over loader hiccup
        return None
    if not peers.peers:
        return None
    if peers.by_court_id(peer_court_id) is None:
        return {
            "error": "unknown_peer",
            "project": project,
            "peer_court_id": peer_court_id,
            "available": [p.court_id for p in peers.peers],
        }
    return None


@mcp.tool()
def grant_peer_access(
    project: str,
    peer_court_id: str,
    paths: list,
    ttl: str = "30m",
    issued_by: str = "",
) -> dict:
    """Mint a temporary *path* grant widening this project's allow_paths.

    Use this when an upstream LLM decides "give Bob's court a 30-min
    look at these specific files". The grant is durable on disk, takes
    effect immediately, and lapses automatically after ``ttl``.

    Args:
        project: this side's project — the grant lives under this
            project's grants/ directory.
        peer_court_id: the remote court's ``court_id`` (as it appears
            in peers.yaml). The grant only applies to messages whose
            ``from_court`` matches this value.
        paths: list of path globs the peer may attach for the duration.
            Same dialect as ``allow_paths`` in court.yaml.
        ttl: how long the grant is valid. Accepts ``"30m"``, ``"1h"``,
            ``"2h30m"``, ``"1d"``, or a bare integer (seconds). Capped
            at 1 year.
        issued_by: optional free-form tag for the audit trail.

    Returns:
        Dict with the grant fields on success, or an ``error`` dict on
        failure. Never raises.
    """
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    if not isinstance(paths, list) or not paths:
        return {"error": "invalid_argument", "detail": "paths must be a non-empty list", "project": project}
    bad_peer = _check_peer_exists(project, peer_court_id)
    if bad_peer is not None:
        return bad_peer
    try:
        grant = lingpai.mint_path_grant(
            project, peer_court_id, list(paths), ttl=ttl, issued_by=issued_by,
        )
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    except OSError as e:
        return {"error": "io_error", "detail": str(e), "project": project}
    return {"project": project, **_grant_to_row(grant)}


@mcp.tool()
def grant_peer_tier(
    project: str,
    peer_court_id: str,
    target_tier: str,
    ttl: str = "30m",
    consume_on_use: bool = False,
    issued_by: str = "",
) -> dict:
    """Mint a temporary *tier* grant overriding a peer's policy tier.

    Useful when you want to wave a single message (or a short stream)
    past the soft-layer review. The grant only raises the peer's
    effective tier; hardcoded denies and user deny_paths still apply.

    Args:
        project: this side's project.
        peer_court_id: peer's court_id as listed in peers.yaml.
        target_tier: one of ``"tier_a"`` / ``"tier_b"`` / ``"tier_c"``.
            Use ``"tier_c"`` to skip judge/human review for the
            duration.
        ttl: how long the grant is valid (same syntax as
            :func:`grant_peer_access`).
        consume_on_use: if True the grant fires exactly once, then
            marks itself consumed; subsequent inbound messages fall
            back to the configured tier.
        issued_by: optional free-form tag.

    Returns:
        Dict with the grant fields on success, or an ``error`` dict on
        failure.
    """
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    bad_peer = _check_peer_exists(project, peer_court_id)
    if bad_peer is not None:
        return bad_peer
    try:
        grant = lingpai.mint_tier_grant(
            project,
            peer_court_id,
            target_tier,
            ttl=ttl,
            consume_on_use=bool(consume_on_use),
            issued_by=issued_by,
        )
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    except OSError as e:
        return {"error": "io_error", "detail": str(e), "project": project}
    return {"project": project, **_grant_to_row(grant)}


@mcp.tool()
def list_grants(project: str) -> dict:
    """Return every grant (active + expired) recorded for the project.

    Useful for "who has access right now?" status queries. The reply
    splits grants into ``active`` and ``expired`` lists so the upstream
    LLM can show only the live ones by default. Each entry includes
    ``grant_type``, ``hit_count`` and ``remaining_seconds`` for quick
    triage.
    """
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    try:
        rows = lingpai.list_grants(project)
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    active: list[dict] = []
    expired: list[dict] = []
    for g in rows:
        (active if g.is_active() else expired).append(_grant_to_row(g))
    return {"project": project, "active": active, "expired": expired}


@mcp.tool()
def grant_info(project: str, grant_id: str) -> dict:
    """Return one grant's full record, including hit_count + remaining time."""
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    try:
        g = lingpai.find_grant(project, grant_id)
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    if g is None:
        return {"error": "unknown_grant", "project": project, "grant_id": grant_id}
    return {
        "project": project,
        "state": "active" if g.is_active() else "expired",
        **_grant_to_row(g),
    }


@mcp.tool()
def revoke_grant(project: str, grant_id: str) -> dict:
    """Delete a grant by id. Returns ``{"ok": true, "result": "revoked"}``
    on success, or an ``error`` dict naming the failure mode."""
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    try:
        result = lingpai.revoke_grant(project, grant_id)
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    if result == "revoked":
        return {"ok": True, "result": "revoked", "project": project, "grant_id": grant_id}
    return {
        "error": result,            # invalid_id | not_found | io_error
        "project": project,
        "grant_id": grant_id,
    }


# ---------------------------------------------------------------------------
# PR-5 shenpi tools — human approval of pending-approval items
# ---------------------------------------------------------------------------


import shenpi as _shenpi


def _item_to_row(item) -> dict:
    return {
        "msg_id": item.msg_id,
        "peer": item.peer,
        "msg_from": item.msg_from,
        "msg_to": item.msg_to,
        "ts_unix": item.ts_unix,
        "reasons": item.reasons,
        "body_excerpt": (item.body or "").strip().splitlines()[0][:300] if item.body else "",
    }


@mcp.tool()
def list_pending(project: str) -> dict:
    """List all留中 (pending-approval) items awaiting human review.

    Reply splits into ``pending`` (active) and ``expired`` (older than
    ``bangjiao.shenpi.timeout_seconds`` — empty when timeout is 0 / unset).
    Each entry includes the message id, peer court_id, reason chain, and
    a short body excerpt; the upstream LLM can surface these to the user
    without reading additional files.
    """
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    try:
        cfg = bangjiao.load_bangjiao(project).shenpi
        listing = _shenpi.list_pending(project, timeout_seconds=cfg.timeout_seconds)
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}
    return {
        "project": project,
        "pending": [_item_to_row(i) for i in listing["pending"]],
        "expired": [_item_to_row(i) for i in listing["expired"]],
    }


@mcp.tool()
def approve_pending(project: str, msg_id: str, action: str, by: str = "") -> dict:
    """Approve or deny one留中 message.

    Args:
        project: receiving court's project name.
        msg_id: the ``id`` frontmatter of the pending file.
        action: one of ``"approve"`` (release to inbox/) or ``"deny"``
            (park in denied/). Anything else returns ``error: invalid_action``.
        by: free-form actor tag for the audit log (e.g. ``alice@laptop``
            or ``wechat-user-bob``). Optional; left empty if unset.

    Approval refuses an expired item (older than
    ``bangjiao.shenpi.timeout_seconds``) with ``error: "expired"`` so a
    forgotten message can't sneak through.

    Returns ``{ok: true, result: "approved"|"denied"}`` on success or an
    ``error`` dict otherwise (``not_found``, ``expired``, ``invalid_action``,
    ``io_error``, ``invalid_argument``).
    """
    if not bangjiao.project_dir(project).is_dir():
        return _bad_project_reply(project)
    if action not in ("approve", "deny"):
        return {"error": "invalid_action", "detail": f"action={action!r} must be 'approve' or 'deny'"}
    try:
        if action == "approve":
            cfg = bangjiao.load_bangjiao(project).shenpi
            result = _shenpi.approve(
                project, msg_id, by=by, timeout_seconds=cfg.timeout_seconds,
            )
        else:
            result = _shenpi.deny(project, msg_id, by=by)
    except ValueError as e:
        return {"error": "invalid_argument", "detail": str(e), "project": project}

    if result in ("approved", "denied"):
        return {"ok": True, "result": result, "project": project, "msg_id": msg_id}
    return {"error": result, "project": project, "msg_id": msg_id}


@mcp.tool()
def list_assigned_issues(state: str = "open", since: Optional[str] = None) -> dict:
    """Return issues assigned to the authenticated Gitea user."""
    try:
        issues = GiteaClient().list_assigned_issues(state=state, since=since)
        return {"issues": issues, "count": len(issues), "fetched_at": _iso_now()}
    except CredentialNotFoundError as e:
        return {"error": "credential_not_found", "detail": str(e)}
    except GiteaClientError as e:
        return {"error": "gitea_error", "detail": str(e)}


@mcp.tool()
def get_issue(repo: str, number: int) -> dict:
    """Return one Gitea issue by repo and number."""
    try:
        return GiteaClient().get_issue(repo, number)
    except CredentialNotFoundError as e:
        return {"error": "credential_not_found", "detail": str(e), "repo": repo, "number": number}
    except GiteaClientError as e:
        return {"error": "gitea_error", "detail": str(e), "repo": repo, "number": number}


@mcp.tool()
def comment_on_issue(repo: str, number: int, body: str) -> dict:
    """Post a comment to a Gitea issue."""
    try:
        return GiteaClient().comment_on_issue(repo, number, body)
    except CredentialNotFoundError as e:
        return {"error": "credential_not_found", "detail": str(e), "repo": repo, "number": number}
    except GiteaClientError as e:
        return {"error": "gitea_error", "detail": str(e), "repo": repo, "number": number}


@mcp.tool()
def transition_issue(repo: str, number: int, state: str) -> dict:
    """Transition a Gitea issue to open/closed."""
    if state not in {"open", "closed"}:
        return {"error": "invalid_state", "detail": f"state={state!r} must be 'open' or 'closed'"}
    try:
        return GiteaClient().transition_issue(repo, number, state)
    except CredentialNotFoundError as e:
        return {"error": "credential_not_found", "detail": str(e), "repo": repo, "number": number}
    except GiteaClientError as e:
        return {"error": "gitea_error", "detail": str(e), "repo": repo, "number": number}


if __name__ == "__main__":
    mcp.run()
