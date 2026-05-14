"""agent-court — 审批 (shenpi): human-approval routing for ``human_required``
messages (PR-5).

Background
----------

The policy engine (lvli.py) routes inbound messages it can't auto-deliver
into ``bus/<peer>/pending-approval/``. Before PR-5 the file just *sat*
there until a human noticed it and ran ``mv`` by hand. PR-5 closes the
loop:

1. When a message lands in ``pending-approval/``, the daemon dispatches
   an outbound notification through each of the configured channels
   (``terminal`` / ``feishu`` / ``wechat``). The notification carries
   enough context (peer, body excerpt, reason chain, message id) for a
   human to decide.
2. The human reviews and acts. Approval/denial flows through one of two
   surfaces — both backed by the same code path here:

   - CLI: ``court-approve <project> approve|deny <id>``
   - MCP tool: ``pizhun(project, id, action)`` (callable from any
     upstream LLM that has the agent-court MCP attached, including a
     claude session bound to WeChat via cc-connect)

3. ``approve`` moves the file into ``bus/<peer>/inbox/`` (delivering it
   to the foreman); ``deny`` moves it into ``bus/<peer>/denied/`` for
   audit. Every action is appended to ``logs/approval-log.jsonl`` so the
   approval record is durable, independent of the notification channel.

Timeout
-------

If ``bangjiao.shenpi.timeout_seconds`` is positive, items older than
that are flagged by :func:`is_expired`, returned in a separate bucket
by :func:`list_pending` (under ``"expired"``), and refused by
:func:`approve` (which returns ``"expired"``). A separate
:func:`sweep_expired` actively moves them to ``denied/`` — meant to
be called periodically (by ``pizhun cleanup``) but is not auto-invoked
to keep the runtime model simple.

Why a separate module
---------------------

Notification + approval is a cross-cutting concern that touches the
daemon, the policy log, three out-of-process channels, and a CLI. A
dedicated module keeps yiguan_daemon, lvli, and the channel implementations
loosely coupled and lets the tests exercise the action layer without
spinning up an HTTP server.
"""

from __future__ import annotations

import json
import re
import asyncio
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# Filename produced by yiguan_daemon's write_inbound_to_bus follows
# ``<unix_ts>-<id>-<from>-to-<to>.md``. The ``id`` is 8 hex chars (see
# peer_lib.gen_id) so we anchor on that.
_PENDING_FILE_RE = re.compile(
    r"^(?P<ts>\d+)-(?P<id>[0-9a-f]{4,16})-(?P<from>[^-]+)-to-(?P<to>[^.]+)\.md$"
)


# ---------------------------------------------------------------------------
# Path helpers (mirror bangjiao.project_dir, scoped to PR-5 paths)
# ---------------------------------------------------------------------------

def _safe_project_dir(project: str) -> Path:
    """Same containment guarantee as ``lingpai._resolve_project_dir``: the
    project name is a safe FS component AND the resolved dir lives inside
    ``$COURT_ROOT/projects/``. Used by every public entry point here."""
    from bangjiao import assert_safe_path_component, court_root, UnsafeNameError

    try:
        assert_safe_path_component(project, field_name="project")
    except UnsafeNameError as e:
        raise ValueError(str(e)) from e

    projects_root = (court_root() / "projects").resolve()
    pdir = (court_root() / "projects" / project).resolve()
    try:
        pdir.relative_to(projects_root)
    except ValueError as e:
        raise ValueError(
            f"project '{project}' resolves outside {projects_root}"
        ) from e
    return pdir


def _bus_dir(project: str) -> Path:
    return _safe_project_dir(project) / "bus"


def _audit_log_path(project: str) -> Path:
    return _safe_project_dir(project) / "logs" / "approval-log.jsonl"


# ---------------------------------------------------------------------------
# PendingItem: a single message awaiting a human verdict
# ---------------------------------------------------------------------------


@dataclass
class PendingItem:
    """One markdown file under ``bus/<peer>/pending-approval/``.

    Fields are parsed from frontmatter + filename; we never trust *only*
    the filename, but if the frontmatter is missing fields we fall back
    to the filename so a hand-written test fixture still works.

    Attributes
    ----------
    project : str
        The receiving court's project name.
    peer : str
        The peer ``court_id`` directory under bus/ that holds this file.
    msg_id : str
        The ``id:`` frontmatter field (same id the sender signed).
    msg_from : str
        ``from:`` frontmatter — the role the peer dispatched as.
    msg_to : str
        ``to:`` frontmatter — the role on this side.
    body : str
        Free-form markdown after the frontmatter. May be empty.
    reasons : list[str]
        ``policy_reasons:`` written by yiguan_daemon. Empty for legacy files.
    ts_unix : int
        Unix timestamp parsed from the filename. Used by ``is_expired``.
    filepath : Path
        Absolute on-disk path.
    """
    project: str
    peer: str
    msg_id: str
    msg_from: str
    msg_to: str
    body: str
    reasons: list[str]
    ts_unix: int
    filepath: Path


def _parse_file(project: str, peer: str, fpath: Path) -> Optional[PendingItem]:
    """Read one .md file. Returns None on malformed frontmatter."""
    try:
        text = fpath.read_text()
    except OSError:
        return None

    if not text.startswith("---\n"):
        return None
    try:
        _, fm, body = text.split("---\n", 2)
    except ValueError:
        return None
    body = body.lstrip("\n")

    fm_data: dict = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm_data[k.strip()] = v.strip()

    # Reasons can be a YAML list or absent — yiguan_daemon writes them as
    # ``policy_reasons: ["a", "b"]`` inline. Parse leniently.
    reasons: list[str] = []
    raw_reasons = fm_data.get("policy_reasons", "")
    if raw_reasons.startswith("[") and raw_reasons.endswith("]"):
        inner = raw_reasons[1:-1].strip()
        for chunk in re.split(r"',\s*'|\",\s*\"", inner):
            chunk = chunk.strip().strip("'\"")
            if chunk:
                reasons.append(chunk)

    m = _PENDING_FILE_RE.match(fpath.name)
    if not m:
        return None

    try:
        ts_unix = int(m.group("ts"))
    except ValueError:
        return None

    return PendingItem(
        project=project,
        peer=peer,
        msg_id=fm_data.get("id") or m.group("id"),
        msg_from=fm_data.get("from") or m.group("from"),
        msg_to=fm_data.get("to") or m.group("to"),
        body=body,
        reasons=reasons,
        ts_unix=ts_unix,
        filepath=fpath,
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_pending(project: str, *, timeout_seconds: int = 0) -> dict:
    """Walk every ``bus/<peer>/pending-approval/`` directory under the
    project and return a dict with two keys: ``"pending"`` (active) and
    ``"expired"`` (older than ``timeout_seconds``; only populated when
    timeout > 0).

    Result is sorted by ts_unix oldest-first so a reviewer's "first up"
    is the one that has been waiting the longest.
    """
    bus = _bus_dir(project)
    if not bus.is_dir():
        return {"pending": [], "expired": []}

    pending: list[PendingItem] = []
    expired: list[PendingItem] = []
    now_unix = int(datetime.now(timezone.utc).timestamp())

    for peer_dir in bus.iterdir():
        if not peer_dir.is_dir():
            continue
        pa = peer_dir / "pending-approval"
        if not pa.is_dir():
            continue
        for f in pa.glob("*.md"):
            item = _parse_file(project, peer_dir.name, f)
            if item is None:
                continue
            if timeout_seconds > 0 and (now_unix - item.ts_unix) >= timeout_seconds:
                expired.append(item)
            else:
                pending.append(item)

    pending.sort(key=lambda i: i.ts_unix)
    expired.sort(key=lambda i: i.ts_unix)
    return {"pending": pending, "expired": expired}


def find_pending(project: str, msg_id: str) -> Optional[PendingItem]:
    """Locate one pending item by id. Returns None if no match."""
    from bangjiao import UnsafeNameError, assert_safe_path_component
    try:
        assert_safe_path_component(msg_id, field_name="msg_id")
    except UnsafeNameError:
        return None
    bus = _bus_dir(project)
    if not bus.is_dir():
        return None
    for peer_dir in bus.iterdir():
        if not peer_dir.is_dir():
            continue
        pa = peer_dir / "pending-approval"
        if not pa.is_dir():
            continue
        for f in pa.glob(f"*-{msg_id}-*.md"):
            item = _parse_file(project, peer_dir.name, f)
            if item is not None and item.msg_id == msg_id:
                return item
    return None


def is_expired(item: PendingItem, *, timeout_seconds: int) -> bool:
    """True iff ``timeout_seconds > 0`` and the item is older than that."""
    if timeout_seconds <= 0:
        return False
    age = int(datetime.now(timezone.utc).timestamp()) - item.ts_unix
    return age >= timeout_seconds


# ---------------------------------------------------------------------------
# Approval actions
# ---------------------------------------------------------------------------


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _audit(project: str, *, action: str, item: PendingItem, by: str,
           extra: Optional[dict] = None) -> None:
    """Append one JSON line to ``logs/approval-log.jsonl``. Best-effort."""
    log = _audit_log_path(project)
    _ensure_dir(log.parent)
    entry = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "action": action,                 # approved | denied | expired | notified | notify_failed
        "by": by,                         # who acted; "$USER@$HOST" or "system"
        "project": project,
        "peer": item.peer,
        "msg_id": item.msg_id,
        "msg_from": item.msg_from,
        "msg_to": item.msg_to,
        "filepath": str(item.filepath),
    }
    if extra:
        entry.update(extra)
    try:
        with log.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # never block delivery on a log-write failure


def approve(project: str, msg_id: str, *, by: str = "",
            timeout_seconds: int = 0) -> str:
    """Approve a pending item. Returns one of:

    - ``"approved"`` — file moved into ``bus/<peer>/inbox/``
    - ``"not_found"`` — no pending item with that id
    - ``"expired"`` — item exceeded ``timeout_seconds``; refuse to approve
    - ``"io_error"`` — move failed (audit log written, original left alone)
    """
    item = find_pending(project, msg_id)
    if item is None:
        return "not_found"
    if is_expired(item, timeout_seconds=timeout_seconds):
        _audit(project, action="approve_refused_expired", item=item, by=by)
        return "expired"

    dest_dir = _bus_dir(project) / item.peer / "inbox"
    _ensure_dir(dest_dir)
    dest = dest_dir / item.filepath.name
    try:
        shutil.move(str(item.filepath), str(dest))
    except OSError as e:
        _audit(project, action="approve_failed", item=item, by=by,
               extra={"error": str(e)})
        return "io_error"
    _audit(project, action="approved",
           item=PendingItem(**{**asdict(item), "filepath": dest}),
           by=by)
    return "approved"


def deny(project: str, msg_id: str, *, by: str = "") -> str:
    """Deny a pending item. Returns one of:

    - ``"denied"`` — file moved into ``bus/<peer>/denied/``
    - ``"not_found"`` — no such id
    - ``"io_error"`` — move failed

    Denying does NOT check ``timeout_seconds`` — denying an old item is
    always allowed.
    """
    item = find_pending(project, msg_id)
    if item is None:
        return "not_found"

    dest_dir = _bus_dir(project) / item.peer / "denied"
    _ensure_dir(dest_dir)
    dest = dest_dir / item.filepath.name
    try:
        shutil.move(str(item.filepath), str(dest))
    except OSError as e:
        _audit(project, action="deny_failed", item=item, by=by,
               extra={"error": str(e)})
        return "io_error"
    _audit(project, action="denied",
           item=PendingItem(**{**asdict(item), "filepath": dest}),
           by=by)
    return "denied"


def sweep_expired(project: str, *, timeout_seconds: int,
                  by: str = "system") -> dict:
    """Auto-deny everything past its TTL. Returns ``{"swept": [ids...]}``.

    Idempotent. No-op when ``timeout_seconds == 0``.
    """
    if timeout_seconds <= 0:
        return {"swept": []}
    listing = list_pending(project, timeout_seconds=timeout_seconds)
    swept: list[str] = []
    for item in listing["expired"]:
        result = deny(project, item.msg_id, by=by)
        if result == "denied":
            swept.append(item.msg_id)
    return {"swept": swept}


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------


def _channel_max_retries(name: str, shenpi_cfg) -> int:
    """Resolve the effective retry budget for one channel.

    Per-channel overrides win; ``None`` falls through to the global
    ``shenpi_cfg.max_retries``. Negative values are clamped to 0.
    """
    block = getattr(shenpi_cfg, name, None)
    per = getattr(block, "max_retries", None) if block is not None else None
    raw = per if per is not None else shenpi_cfg.max_retries
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


async def _try_channel_with_retry(name: str, item: PendingItem,
                                  shenpi_cfg) -> str:
    """Fire one channel; retry on exception with exponential backoff.

    Returns ``"ok"`` on first successful delivery, ``"unknown_channel"``
    if the name isn't registered, or ``f"error: <last-exception>"``
    when all retries are exhausted. Each individual attempt (success or
    failure) gets one line in ``approval-log.jsonl`` as a
    ``"notified"`` / ``"notify_attempt_failed"`` event. After
    exhausting retries we add one ``"notify_failed"`` summary line so a
    reader can scan for terminal failures without reading every attempt.

    Backoff between failed attempts:
    ``shenpi_cfg.backoff_seconds * 2**attempt``.
    """
    sender = _CHANNEL_TABLE.get(name)
    if sender is None:
        _audit(item.project, action="notify_failed", item=item, by="system",
               extra={"channel": name, "error": "unknown_channel"})
        return "unknown_channel"

    max_retries = _channel_max_retries(name, shenpi_cfg)
    backoff = max(float(shenpi_cfg.backoff_seconds), 0.0)
    last_error: Optional[str] = None
    total_attempts = max_retries + 1

    for attempt in range(total_attempts):
        try:
            await sender(item, shenpi_cfg)
            _audit(item.project, action="notified", item=item, by="system",
                   extra={"channel": name, "attempt": attempt + 1})
            return "ok"
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            _audit(item.project, action="notify_attempt_failed", item=item,
                   by="system",
                   extra={"channel": name, "attempt": attempt + 1,
                          "error": last_error})
            if attempt < total_attempts - 1:
                # Exponential backoff. ``await``-able so the event loop
                # stays free.
                await asyncio.sleep(backoff * (2 ** attempt))

    _audit(item.project, action="notify_failed", item=item, by="system",
           extra={"channel": name, "attempts": total_attempts,
                  "error": last_error})
    return f"error: {last_error}"


async def notify(item: PendingItem, *, shenpi_cfg) -> dict:
    """Dispatch a notification through the configured channels.

    PR-6 added two delivery policies on top of the PR-5 fan-out:

    - ``broadcast`` (default): fire every channel concurrently. Each
      channel retries independently on transient failure. Useful when
      you actively *want* to get pinged on multiple devices.
    - ``escalate``: walk ``channels`` in order; stop on the first
      channel that delivers successfully. Each channel exhausts its
      retry budget before we fall through to the next. Useful when
      the channel list is ranked by preference (e.g. Feishu first,
      fall back to WeChat if Feishu's webhook is down).

    Returns a dict mapping channel name → result, where result is
    ``"ok"`` / ``"unknown_channel"`` / ``f"error: <msg>"``. When the
    policy is ``escalate`` and a later channel was skipped (because an
    earlier one succeeded), that channel does NOT appear in the
    return dict.

    Caller (the daemon) should treat this as fire-and-forget; do NOT
    await its result inside a request handler — schedule it on the
    event loop instead.
    """
    policy = getattr(shenpi_cfg, "delivery_policy", "broadcast")

    if policy == "escalate":
        out: dict[str, str] = {}
        for name in shenpi_cfg.channels:
            result = await _try_channel_with_retry(name, item, shenpi_cfg)
            out[name] = result
            if result == "ok":
                break
        return out

    # broadcast: fire all in parallel; each channel's retry budget runs
    # independently. We collect results in stable channel order so the
    # caller's mental model matches the config list.
    tasks = [
        asyncio.create_task(_try_channel_with_retry(name, item, shenpi_cfg))
        for name in shenpi_cfg.channels
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out_b: dict[str, str] = {}
    for name, r in zip(shenpi_cfg.channels, results):
        if isinstance(r, Exception):
            out_b[name] = f"error: {r}"
        else:
            out_b[name] = r
    return out_b


# Populated by the channels package at import time so we can keep imports
# late and avoid a circular dependency through bangjiao.
_CHANNEL_TABLE: dict = {}


def _register_channel(name: str, sender) -> None:
    """Used by ``shenpi_channels.<name>`` modules to register themselves."""
    _CHANNEL_TABLE[name] = sender


# Eagerly import the channels so they self-register when shenpi is loaded.
# Done at the bottom so any forward references resolve first.
import shenpi_channels.terminal  # noqa: E402, F401
import shenpi_channels.feishu    # noqa: E402, F401
import shenpi_channels.wechat    # noqa: E402, F401
