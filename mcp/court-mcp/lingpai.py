"""agent-court — temporary path-access grants (PR-4).

A *grant* is a project-scoped record saying "for the next N minutes, this
peer court is allowed to do <something> on inbound messages". It extends
``court.yaml``'s static authorization at runtime, without anyone editing
yaml.

Two flavors, distinguished by ``grant_type``:

- ``"path"`` — widens ``allow_paths`` so a peer may attach extra files.
  This is the original PR-4 use case.
- ``"tier"`` — temporarily upgrades a peer's policy tier (e.g. tier_a → tier_c)
  for the duration of the grant. Optional ``consume_on_use=True`` makes
  the grant fire exactly once (subsequent messages fall back to the
  configured tier).

The intended workflow mirrors sudo:

.. code-block:: bash

    # Path widening
    banling example bob-laptop-example notes/2026-Q2.md --ttl 30m
    # ^ Bob may now attach 'notes/2026-Q2.md' for the next 30 min.

    # Tier upgrade, one shot
    banling example bob-laptop-example --tier tier_c --once
    # ^ Next inbound message from Bob skips human-review.

    banling example list
    banling example info <grant-id>
    banling example revoke <grant-id>

Storage
-------

One JSON file per grant, under
``$COURT_ROOT/projects/<p>/grants/<grant-id>.json``. A separate file
per grant means ``banling list`` is just ``ls`` and revoke is just
``rm`` — no central index, nothing to corrupt. Writes are atomic
(``tempfile + os.replace``) so a reader never sees a half-written
record even under concurrent mint/load.

Security model
--------------

A grant ONLY adds capabilities; it cannot subtract. Neither path nor
tier grants can:

- override ``HARDCODED_DENY_PATHS`` (system secrets stay blocked);
- override ``deny_paths`` from ``court.yaml`` (user blacklist wins);
- override ``HARDCODED_KEYWORDS`` (sensitive body content still
  triggers ``human_required``).

Tier grants only relax the *tier-derived* action (the soft layer in
``lvli.evaluate``). Path grants only relax the *allow_paths* check.

Granularity is ``(peer_court_id, [paths] | target_tier)`` — the same
peer entry that matches ``from_court`` on inbound. Per-role grants
(e.g. "only Bob's foreman") are deliberately out of scope; the
``expose_roles`` whitelist already covers that.

Path containment
----------------

All grant entry points run ``project`` through
:func:`_resolve_project_dir`, which validates the name as a safe path
component AND verifies the resolved directory lives inside
``$COURT_ROOT/projects/``. A caller supplying ``project="../etc"``
gets ``ValueError`` instead of arbitrary filesystem access. This
hardens *only* the PR-4 surface; pre-existing helpers in
:mod:`peer_lib` and :mod:`server` retain their existing behavior.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Limits + constants
# ---------------------------------------------------------------------------

# Upper bound on TTL: one year. Beyond this we consider the input pathological
# and refuse, so ``datetime + ttl`` arithmetic can never overflow.
MAX_TTL_SECONDS = 365 * 24 * 3600

# Per-grant JSON file size guard. A normal grant is < 1 KB; anything past
# this is treated as garbage and skipped (with a warning to peer-errors.log).
MAX_GRANT_FILE_BYTES = 64 * 1024

VALID_TIERS = ("tier_a", "tier_b", "tier_c")
VALID_GRANT_TYPES = ("path", "tier")


# ---------------------------------------------------------------------------
# Path helpers (with containment check)
# ---------------------------------------------------------------------------

def _resolve_project_dir(project: str) -> Path:
    """Return the project's on-disk dir if it lives strictly inside
    ``$COURT_ROOT/projects/``. Raises ``ValueError`` otherwise.

    The guard is twofold:

    1. ``project`` itself must be a single safe path component (no slashes,
       no ``..``, only ``[A-Za-z0-9._-]``). This is enforced via
       :func:`bangjiao.assert_safe_path_component`.
    2. The resolved directory must be a strict descendant of the projects
       root. A symlink that resolves outside the root counts as "outside"
       — better safe than reading via a hostile symlink.

    Used by every PR-4 entry point that takes a ``project`` string.
    """
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


def grants_dir(project: str) -> Path:
    """Return ``<project_dir>/grants``. Validates path containment."""
    return _resolve_project_dir(project) / "grants"


def _peer_errors_log(project: str) -> Path:
    from bangjiao import project_peer_errors_log
    return project_peer_errors_log(project)


def _warn(project: str, message: str) -> None:
    """Append a warning to ``logs/peer-errors.log``. Never raises."""
    try:
        log = _peer_errors_log(project)
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with log.open("a") as f:
            f.write(f"[{ts}] grants: {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Grant:
    """A single time-bounded grant for one peer court.

    Common fields across grant types
    --------------------------------
    id : str
        Random 8-char hex identifier. Used as filename and revoke handle.
    grant_type : str
        ``"path"`` (default, widens allow_paths) or ``"tier"``
        (overrides peer_tier).
    granted_to : str
        ``court_id`` of the peer this grant applies to. Must match
        ``from_court`` on an inbound message for the grant to apply.
    issued_ts, expires_ts : str
        ISO 8601 timestamps. Issuance and expiry.
    issued_by : str
        Free-form human-readable hint (e.g. ``alice@laptop``).
        Truncated to 128 chars. Goes into the audit trail.
    consumed_ts : str or None
        For ``consume_on_use`` grants — set to the consumption time after
        the grant fires once. Consumed grants behave as expired.
    hit_count : int
        Number of inbound messages this grant has matched. Updated by
        ``record_hit``. Used by ``banling info`` for diagnostics.
    last_hit_ts : str or None
        ISO timestamp of the most recent hit; None if never used.

    Path-grant fields
    -----------------
    paths : list[str]
        Path globs the peer may attach. Same dialect as
        ``allow_paths`` in court.yaml (``**/X`` understood, absolute
        paths and ``..`` segments are rejected by
        ``lvli.normalize_attach`` regardless of what's here).

    Tier-grant fields
    -----------------
    target_tier : str or None
        The tier to use in place of the peer's configured tier. One of
        ``tier_a``/``tier_b``/``tier_c``.
    consume_on_use : bool
        If True, the grant marks itself consumed after the first hit.
    """
    id: str
    granted_to: str
    issued_ts: str
    expires_ts: str
    grant_type: str = "path"
    paths: list[str] = field(default_factory=list)
    target_tier: Optional[str] = None
    consume_on_use: bool = False
    consumed_ts: Optional[str] = None
    hit_count: int = 0
    last_hit_ts: Optional[str] = None
    issued_by: str = ""

    def is_active(self, *, now: Optional[datetime] = None) -> bool:
        if self.consumed_ts is not None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_ts)
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now is None:
            now = datetime.now(timezone.utc)
        return now < exp

    def remaining_seconds(self, *, now: Optional[datetime] = None) -> int:
        """Seconds until ``expires_ts``. Negative if past expiry, 0 if consumed."""
        if self.consumed_ts is not None:
            return 0
        try:
            exp = datetime.fromisoformat(self.expires_ts)
        except ValueError:
            return 0
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now is None:
            now = datetime.now(timezone.utc)
        return int((exp - now).total_seconds())


# ---------------------------------------------------------------------------
# TTL parser
# ---------------------------------------------------------------------------

_TTL_PART_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>[smhd])", re.IGNORECASE)
_TTL_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(spec) -> int:
    """Parse ``"30m"``, ``"1h"``, ``"2h30m"``, ``"1d"`` → seconds.

    Accepts an int (seconds) directly. Raises ``ValueError`` for garbage
    or values outside ``[1, MAX_TTL_SECONDS]``.
    """
    if isinstance(spec, int) and not isinstance(spec, bool):
        if spec < 1:
            raise ValueError(f"ttl must be ≥ 1 second, got {spec}")
        if spec > MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl {spec} exceeds max of {MAX_TTL_SECONDS} seconds (1 year)"
            )
        return spec
    if not isinstance(spec, str):
        raise ValueError(f"ttl must be a string or int, got {type(spec).__name__}")
    spec = spec.strip().lower()
    if not spec:
        raise ValueError("ttl is empty")
    if spec.isdigit():
        return parse_ttl(int(spec))
    total = 0
    consumed = 0
    for m in _TTL_PART_RE.finditer(spec):
        total += int(m.group("num")) * _TTL_UNIT_SECS[m.group("unit").lower()]
        consumed += len(m.group(0))
    if total == 0 or consumed != len("".join(spec.split())):
        raise ValueError(
            f"ttl {spec!r} not recognized — use forms like '30m', '1h', '2h30m', '1d'"
        )
    if total > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl {spec!r} resolves to {total}s, exceeds max of {MAX_TTL_SECONDS}s"
        )
    return total


# ---------------------------------------------------------------------------
# Minting + persistence
# ---------------------------------------------------------------------------

def _new_grant_id() -> str:
    """8 hex chars — 32-bit space, fine for ~10⁴ live grants per project.
    The daemon never auto-mints, only humans/MCP tools do."""
    return secrets.token_hex(4)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to ``path`` atomically: tempfile in same dir + ``os.replace``.

    A reader iterating ``glob("*.json")`` either sees the old content or
    the new content — never a partially-written file. The temp file is
    not visible to ``*.json`` because we use a non-matching suffix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tf.flush()
        try:
            os.fsync(tf.fileno())
        except OSError:
            pass
    try:
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _compute_expiry(ttl_seconds: int) -> str:
    """Return ISO-formatted expiry. ``ttl_seconds`` is already bounded by
    ``parse_ttl``, so ``timedelta`` arithmetic cannot raise here."""
    now = _now_local()
    expires = now + timedelta(seconds=ttl_seconds)
    return expires.isoformat(timespec="seconds")


def _validate_paths(paths) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list")
    cleaned: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"path entry not a non-empty string: {p!r}")
        cleaned.append(p.strip())
    return cleaned


def _validate_granted_to(granted_to: str) -> str:
    from bangjiao import assert_safe_path_component
    assert_safe_path_component(granted_to, field_name="granted_to")
    return granted_to


def mint_path_grant(
    project: str,
    granted_to: str,
    paths: list[str],
    *,
    ttl,
    issued_by: str = "",
) -> Grant:
    """Mint a path-widening grant and persist atomically."""
    gdir = grants_dir(project)
    _validate_granted_to(granted_to)
    cleaned_paths = _validate_paths(paths)
    ttl_seconds = parse_ttl(ttl)

    now = _now_local()
    grant = Grant(
        id=_new_grant_id(),
        granted_to=granted_to,
        issued_ts=now.isoformat(timespec="seconds"),
        expires_ts=_compute_expiry(ttl_seconds),
        grant_type="path",
        paths=cleaned_paths,
        target_tier=None,
        consume_on_use=False,
        issued_by=str(issued_by or "")[:128],
    )

    _atomic_write_json(gdir / f"{grant.id}.json", asdict(grant))
    return grant


def mint_tier_grant(
    project: str,
    granted_to: str,
    target_tier: str,
    *,
    ttl,
    consume_on_use: bool = False,
    issued_by: str = "",
) -> Grant:
    """Mint a tier-override grant.

    ``consume_on_use=True`` makes the grant fire exactly once: after the
    first inbound message uses it, ``consumed_ts`` is set and subsequent
    messages fall back to the configured tier.
    """
    gdir = grants_dir(project)
    _validate_granted_to(granted_to)
    if target_tier not in VALID_TIERS:
        raise ValueError(f"target_tier must be one of {VALID_TIERS}, got {target_tier!r}")
    ttl_seconds = parse_ttl(ttl)

    now = _now_local()
    grant = Grant(
        id=_new_grant_id(),
        granted_to=granted_to,
        issued_ts=now.isoformat(timespec="seconds"),
        expires_ts=_compute_expiry(ttl_seconds),
        grant_type="tier",
        paths=[],
        target_tier=target_tier,
        consume_on_use=bool(consume_on_use),
        issued_by=str(issued_by or "")[:128],
    )

    _atomic_write_json(gdir / f"{grant.id}.json", asdict(grant))
    return grant


def mint_grant(
    project: str,
    granted_to: str,
    paths: list[str],
    *,
    ttl,
    issued_by: str = "",
) -> Grant:
    """Backward-compatible alias for :func:`mint_path_grant`.

    Older callers (and the original MCP tool surface) use the 3-positional
    form ``mint_grant(project, granted_to, paths, ttl=...)``. Keep this
    shape working so the PR-4 → PR-4.1 transition doesn't break anyone.
    """
    return mint_path_grant(project, granted_to, paths, ttl=ttl, issued_by=issued_by)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_grant_file(p: Path, *, project: Optional[str] = None) -> Optional[Grant]:
    """Strictly parse a grant file. Returns None on any schema violation
    or oversize file; in those cases an entry is appended to
    ``logs/peer-errors.log`` so an admin can see something's wrong.
    """
    try:
        size = p.stat().st_size
    except OSError as e:
        if project is not None:
            _warn(project, f"stat {p.name} failed: {e}")
        return None
    if size > MAX_GRANT_FILE_BYTES:
        if project is not None:
            _warn(
                project,
                f"grant file {p.name} is {size} bytes — exceeds "
                f"{MAX_GRANT_FILE_BYTES}, skipping",
            )
        return None
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        if project is not None:
            _warn(project, f"unparseable grant file {p.name}: {e}")
        return None
    if not isinstance(raw, dict):
        if project is not None:
            _warn(project, f"grant file {p.name}: top-level is not an object")
        return None

    try:
        # Required string fields.
        gid = raw["id"]
        granted_to = raw["granted_to"]
        issued_ts = raw["issued_ts"]
        expires_ts = raw["expires_ts"]
        if not all(isinstance(v, str) for v in (gid, granted_to, issued_ts, expires_ts)):
            raise TypeError("required fields must all be strings")

        # paths: defaults to empty list, must be list[str] when present.
        paths_raw = raw.get("paths", [])
        if not isinstance(paths_raw, list):
            raise TypeError("paths must be a list")
        paths: list[str] = []
        for p_entry in paths_raw:
            if not isinstance(p_entry, str):
                raise TypeError(f"paths contains non-string {p_entry!r}")
            paths.append(p_entry)

        # Optional / defaulted fields.
        grant_type = raw.get("grant_type", "path")
        if grant_type not in VALID_GRANT_TYPES:
            raise ValueError(f"unknown grant_type {grant_type!r}")
        target_tier = raw.get("target_tier")
        if target_tier is not None and target_tier not in VALID_TIERS:
            raise ValueError(f"unknown target_tier {target_tier!r}")
        consume_on_use = bool(raw.get("consume_on_use", False))
        consumed_ts_raw = raw.get("consumed_ts")
        consumed_ts: Optional[str] = (
            consumed_ts_raw if isinstance(consumed_ts_raw, str) else None
        )
        hit_count = int(raw.get("hit_count", 0) or 0)
        last_hit_ts_raw = raw.get("last_hit_ts")
        last_hit_ts: Optional[str] = (
            last_hit_ts_raw if isinstance(last_hit_ts_raw, str) else None
        )
        issued_by = str(raw.get("issued_by", "") or "")

        # Type-specific invariants.
        if grant_type == "path" and not paths:
            raise ValueError("path grant has empty paths list")
        if grant_type == "tier" and target_tier is None:
            raise ValueError("tier grant missing target_tier")

        return Grant(
            id=gid,
            granted_to=granted_to,
            issued_ts=issued_ts,
            expires_ts=expires_ts,
            grant_type=grant_type,
            paths=paths,
            target_tier=target_tier,
            consume_on_use=consume_on_use,
            consumed_ts=consumed_ts,
            hit_count=hit_count,
            last_hit_ts=last_hit_ts,
            issued_by=issued_by,
        )
    except (KeyError, TypeError, ValueError) as e:
        if project is not None:
            _warn(project, f"grant file {p.name}: schema mismatch: {e}")
        return None


def list_grants(project: str) -> list[Grant]:
    """Return every grant on disk for the project (active + expired).

    Sorted by ``issued_ts`` so ``banling list`` shows newest last.
    Malformed JSON files are skipped (and an entry is appended to
    ``logs/peer-errors.log``) — we never refuse to list grants because
    of one corrupted entry.
    """
    gdir = grants_dir(project)
    if not gdir.is_dir():
        return []
    out: list[Grant] = []
    for f in gdir.glob("*.json"):
        # Skip our atomic-write temp files (dotfile prefix).
        if f.name.startswith("."):
            continue
        g = _read_grant_file(f, project=project)
        if g is not None:
            out.append(g)
    out.sort(key=lambda g: g.issued_ts)
    return out


def load_active_grants(project: str) -> list[Grant]:
    """Like :func:`list_grants` but filters out anything past TTL or consumed."""
    now = datetime.now(timezone.utc)
    return [g for g in list_grants(project) if g.is_active(now=now)]


def find_grant(project: str, grant_id: str) -> Optional[Grant]:
    """Return a single grant by id (active or expired). None if not found."""
    from bangjiao import UnsafeNameError, assert_safe_path_component

    try:
        assert_safe_path_component(grant_id, field_name="grant_id")
    except UnsafeNameError:
        return None
    fpath = grants_dir(project) / f"{grant_id}.json"
    if not fpath.is_file():
        return None
    return _read_grant_file(fpath, project=project)


def load_grants_for_peer(project: str, peer_court_id: str) -> list[str]:
    """Return the union of allowed path globs from every active *path* grant
    addressed to ``peer_court_id``. Empty list if none.

    This is the shape :func:`lvli.evaluate` historically expected — a
    flat list of globs to OR into ``allow_paths``. Tier grants are
    intentionally excluded here; callers that want tier overrides
    should use :func:`load_effective_tier_grant`.
    """
    out: list[str] = []
    for g in load_active_grants(project):
        if g.granted_to == peer_court_id and g.grant_type == "path":
            out.extend(g.paths)
    return out


def load_path_grants_for_peer(project: str, peer_court_id: str) -> list[Grant]:
    """Return the structured list of active *path* grants for a peer.

    Same as :func:`load_grants_for_peer` but returns Grant objects so the
    caller can attribute matches back to specific grant ids (for
    ``record_hit``).
    """
    return [
        g for g in load_active_grants(project)
        if g.granted_to == peer_court_id and g.grant_type == "path"
    ]


# Tier priority (higher index = more permissive). When more than one
# active tier grant exists for a peer, the most permissive wins so the
# most recently issued one of equal priority is chosen.
_TIER_PRIORITY = {"tier_a": 0, "tier_b": 1, "tier_c": 2}


def load_effective_tier_grant(project: str, peer_court_id: str) -> Optional[Grant]:
    """Return the active tier grant that wins for this peer, or None.

    Resolution: among all active tier grants addressed to ``peer_court_id``,
    pick the one with the highest ``target_tier`` priority
    (tier_c > tier_b > tier_a). Ties broken by latest ``issued_ts``.
    """
    candidates = [
        g for g in load_active_grants(project)
        if g.granted_to == peer_court_id and g.grant_type == "tier"
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda g: (_TIER_PRIORITY.get(g.target_tier or "", -1), g.issued_ts),
        reverse=True,
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Hit tracking + consumption
# ---------------------------------------------------------------------------

def _rewrite_grant(project: str, grant: Grant) -> bool:
    """Atomically overwrite a grant's JSON. Returns False on failure."""
    fpath = grants_dir(project) / f"{grant.id}.json"
    if not fpath.is_file():
        return False
    try:
        _atomic_write_json(fpath, asdict(grant))
    except OSError as e:
        _warn(project, f"rewrite {grant.id}.json failed: {e}")
        return False
    return True


def record_hit(project: str, grant_id: str) -> bool:
    """Increment ``hit_count`` + refresh ``last_hit_ts`` on the named grant.

    Idempotency / concurrency: read → mutate → atomic write. If two
    daemons happen to race on the same grant, one update may be lost;
    the consequence is a slightly understated hit_count, which is fine
    for what is an audit-trail nicety.
    """
    g = find_grant(project, grant_id)
    if g is None:
        return False
    g.hit_count += 1
    g.last_hit_ts = _now_local().isoformat(timespec="seconds")
    return _rewrite_grant(project, g)


def mark_consumed(project: str, grant_id: str) -> bool:
    """Set ``consumed_ts`` on a once-grant after it has fired."""
    g = find_grant(project, grant_id)
    if g is None:
        return False
    if g.consumed_ts is not None:
        return True
    g.consumed_ts = _now_local().isoformat(timespec="seconds")
    return _rewrite_grant(project, g)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def revoke_grant(project: str, grant_id: str) -> str:
    """Delete a grant file. Returns one of:

    - ``"revoked"`` — file removed.
    - ``"invalid_id"`` — id contained unsafe characters.
    - ``"not_found"`` — no such grant on disk.
    - ``"io_error"`` — unlink failed.

    Splits the previous bool return so the CLI/MCP layer can surface a
    useful reason instead of a flat "no such grant".
    """
    from bangjiao import UnsafeNameError, assert_safe_path_component

    try:
        assert_safe_path_component(grant_id, field_name="grant_id")
    except UnsafeNameError:
        return "invalid_id"
    fpath = grants_dir(project) / f"{grant_id}.json"
    if not fpath.is_file():
        return "not_found"
    try:
        fpath.unlink()
    except OSError as e:
        _warn(project, f"unlink {grant_id}.json failed: {e}")
        return "io_error"
    return "revoked"
