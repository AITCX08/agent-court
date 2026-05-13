"""agent-court — policy engine (PR-2).

Decides what to do with an inbound peer message *after* signature
verification and role-whitelist checks have already passed.

Decision actions
----------------
- ``auto_pass``      drop the message straight into ``bus/<peer>/inbox/``.
  Foreman picks it up via the existing court-watcher routing.
- ``judge``          (PR-2 stub) pass through to inbox + emit warning log.
  PR-3 will replace this branch with an llm_judge call that returns a
  confidence score and may downgrade to ``auto_pass`` or upgrade to
  ``human_required``.
- ``human_required`` park in ``bus/<peer>/pending-approval/`` and do
  *not* deliver until a human explicitly moves the file to ``inbox/``.
  PR-5 wires this branch to multi-channel approvals (terminal +
  FeiShu + WeChat).
- ``denied``         park in ``bus/<peer>/denied/`` for audit and stop.
  Never reaches the foreman, ever.

Rule layers
-----------
**Hard rules** — written in code, NOT overridable from ``policy.yaml``.
These exist so that a misconfigured ``policy.yaml`` cannot accidentally
expose system-level secrets.

1. ``HARDCODED_DENY_PATHS`` (e.g. ``**/.ssh/**``, ``**/.env``,
   ``**/id_rsa*``). Any attach matching one of these → ``denied``.
2. ``HARDCODED_KEYWORDS``  (e.g. ``password``, ``api_key``, ``sk-``).
   Any case-insensitive substring match in ``body`` → upgrade to
   ``human_required``.

**Project rules** — read from ``court.yaml`` (paths) and
``policy.yaml`` (tiers + extra keywords).

3. User ``deny_paths`` in ``court.yaml`` → ``denied``.
4. User ``allow_paths`` in ``court.yaml`` non-empty: every attach must
   match at least one allow glob, otherwise → ``human_required``.
5. Extra ``sensitive_keywords`` from ``policy.yaml`` are appended to
   the built-in list at evaluation time.

**Soft tier** — the final layer when nothing harder fires:

6. ``peers.yaml`` may pin ``policy_tier`` per peer. If absent, the
   ``policy.yaml`` ``default_tier`` applies. The tier maps to an
   action: ``tier_a → human_required``, ``tier_b → judge``,
   ``tier_c → auto_pass``.

Evaluation order
----------------
Hard layer first (1 → 2 → 3 → 4 → 5) so a single matching deny path
short-circuits everything; soft layer last (6). The first rule to fire
wins; reasons accumulate so the audit log can show *why* a decision
was made.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml


# ---------------------------------------------------------------------------
# Hardcoded layer
# ---------------------------------------------------------------------------

# Paths that no policy.yaml can re-allow. These are common locations for
# system secrets; if an inbound peer message attaches one of these we treat
# it as a deliberate attempt to exfiltrate. Patterns are matched
# case-insensitively against a normalized POSIX path with `..` segments
# and absolute leading slashes already stripped — see ``_match_any``.
HARDCODED_DENY_PATHS: tuple[str, ...] = (
    # OS-level secret bundles
    "etc/**",                          # /etc/* (leading / stripped after normalize)
    "root/**",                         # /root/*
    "var/lib/docker/**",
    "var/run/docker.sock",
    # User ssh / GPG
    "**/.ssh/**",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.gnupg/**",
    # Environment / shell secrets
    "**/.env",
    "**/.env.*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.dockercfg",
    "**/credentials.json",
    "**/secrets/**",
    # Cloud SDKs
    "**/.aws/**",
    "**/.azure/**",
    "**/.gcp/**",
    "**/.config/gcloud/**",
    "**/.kube/config",
    # Generic key file extensions
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    # macOS keychains
    "**/Library/Keychains/**",
)

# Substrings (case-insensitive) whose appearance in ``body`` forces a
# human to look at the message. Not overridable from config; only
# extendable via ``policy.yaml`` ``sensitive_keywords:``.
HARDCODED_KEYWORDS: tuple[str, ...] = (
    "api_key", "apikey", "api-key",
    "password", "passwd",
    "secret", "token", "auth_token",
    "private_key", "privatekey",
    "AKIA",   # AWS access key prefix
    "sk-",    # OpenAI / Anthropic key prefix
)


# Tier → action mapping. Unknown tier defaults to the safest action.
_TIER_ACTION: dict[str, str] = {
    "tier_a": "human_required",
    "tier_b": "judge",
    "tier_c": "auto_pass",
}

# Priority for "which tier is more permissive". Used by tier_grant logic
# in :func:`evaluate` and by lingpai.load_effective_tier_grant.
_TIER_PRIORITY: dict[str, int] = {"tier_a": 0, "tier_b": 1, "tier_c": 2}


# ---------------------------------------------------------------------------
# Config + decision dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PolicyConfig:
    default_tier: str = "tier_b"
    extra_keywords: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str                       # auto_pass | judge | human_required | denied
    tier: str                         # tier_a/b/c, or "hard_rule" when a hard layer fired
    reasons: list[str] = field(default_factory=list)
    # PR-4.1 — grant ids whose paths/tier matched during evaluation. The
    # daemon iterates this list after evaluate() to call lingpai.record_hit
    # (and lingpai.mark_consumed for consume_on_use tier grants).
    grant_hits: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_policy(project: str) -> PolicyConfig:
    """Read ``$COURT_ROOT/projects/<p>/policy.yaml``.

    Missing file, empty file, or malformed yaml all collapse to defaults
    (``tier_b`` + no extra keywords) so a half-set-up project keeps
    working — failing closed here would also kill the receiver, which is
    worse than mildly-permissive defaults that already get upgraded by
    the hardcoded layer.
    """
    # Local import to keep policy.py importable in environments that don't
    # have peer_lib's full dependency graph available (e.g. tests that
    # exercise pure logic).
    from bangjiao import project_dir

    cfg_path = project_dir(project) / "policy.yaml"
    if not cfg_path.is_file():
        return PolicyConfig()

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        return PolicyConfig()

    return PolicyConfig(
        default_tier=raw.get("default_tier") or "tier_b",
        extra_keywords=list(raw.get("sensitive_keywords") or []),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

# Windows drive prefix like "C:" — treated as absolute and rejected.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_attach(path) -> Optional[str]:
    """Canonicalize an ``attaches`` entry; return ``None`` if it is unsafe
    enough that we shouldn't even try to match it against globs.

    Rules:
    - non-strings, empty strings: reject
    - absolute paths (``/``, ``~``, ``\\``, ``C:``): strip the prefix and
      treat the remainder as relative — that way ``/etc/passwd`` still
      matches the hardcoded ``etc/**`` pattern, but the path *as written*
      can never escape ``bus/``
    - paths whose normalized form contains any ``..`` segment: reject
      entirely (no honest workflow needs to escape upward and we'd
      rather over-flag than parse upward references)
    - backslashes are converted to forward slashes so a Windows-style
      path can't slip past a posix glob
    """
    if not isinstance(path, str) or not path.strip():
        return None
    cleaned = path.replace("\\", "/").strip()

    # Strip leading absolute markers; remaining path is treated as
    # relative for matching purposes.
    while cleaned.startswith(("/", "~")):
        cleaned = cleaned[1:]
    if _WINDOWS_DRIVE_RE.match(cleaned):
        cleaned = cleaned[2:].lstrip("/")
    if not cleaned:
        return None

    # posixpath.normpath collapses ``a/./b`` → ``a/b`` and ``a/b/../c`` →
    # ``a/c``. We use that to detect *whether* the original path tried to
    # walk upward; we don't trust the collapsed form to be safe because a
    # path can normalize cleanly yet still have *intended* upward reach
    # like ``foo/../../bar``. Easier rule: reject anything that has any
    # ``..`` component in the un-normalized split.
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None

    return "/".join(parts)


def _match_any(path: str, patterns: Iterable[str]) -> Optional[str]:
    """Return the first pattern that matches ``path``, else None.

    Matching is case-insensitive on lowercase strings so a deny rule
    like ``**/.ssh/**`` catches both ``.ssh/id_rsa`` and ``.SSH/ID_RSA``
    regardless of the host filesystem's case sensitivity.

    ``fnmatch`` doesn't natively understand the globstar ``**`` meaning
    "zero or more path segments" — its ``**`` is just two ``*`` in a
    row. We compensate by also testing the pattern with leading ``**/``
    and trailing ``/**`` stripped, which between them cover the common
    "match at any depth" intent (``**/.npmrc`` should also catch
    ``.npmrc`` at the root; ``etc/**`` should also catch the bare
    ``etc`` directory name).
    """
    lowered = path.lower()
    for pat in patterns:
        pat_l = pat.lower()
        # 1. Direct fnmatch against the full path.
        if fnmatch.fnmatchcase(lowered, pat_l):
            return pat
        # 2. Strip leading ``**/`` and retry — handles "no segment before
        #    the named suffix" (e.g. ``.npmrc`` against ``**/.npmrc``).
        if pat_l.startswith("**/") and fnmatch.fnmatchcase(lowered, pat_l[3:]):
            return pat
        # 3. Strip trailing ``/**`` and retry — handles "bare directory
        #    name" (e.g. ``etc`` against ``etc/**``).
        if pat_l.endswith("/**") and fnmatch.fnmatchcase(lowered, pat_l[:-3]):
            return pat
    return None


def evaluate(
    msg: dict,
    *,
    peer_tier: Optional[str],
    policy: PolicyConfig,
    allow_paths: list[str],
    deny_paths: list[str],
    grant_paths: Optional[list[str]] = None,
    path_grants: Optional[list] = None,
    tier_grant=None,
) -> Decision:
    """Return the policy ``Decision`` for an inbound message.

    Parameters
    ----------
    msg : dict
        The verified inbound message. Reads ``body`` and ``attaches``.
        ``attaches`` is optional; missing → empty list.
    peer_tier : str or None
        Per-peer tier override from ``peers.yaml``. None → use
        ``policy.default_tier``.
    policy : PolicyConfig
        Loaded from ``policy.yaml``.
    allow_paths, deny_paths : list[str]
        User-configured globs from ``court.yaml`` ``bangjiao:`` block.
        HARDCODED_DENY_PATHS is checked in addition (not as a
        replacement).
    grant_paths : list[str] or None
        PR-4 legacy shape — flat list of path globs already-flattened
        from active lingpai. When provided, hits do NOT show up in
        ``decision.grant_hits``. Prefer ``path_grants`` for new callers.
    path_grants : list[Grant] or None
        PR-4.1 — structured list of active path grants for this peer.
        Each grant's paths are OR'd into the effective allow_paths; when
        an attach matches a pattern from a specific grant, that grant's
        id is appended to ``decision.grant_hits`` so the daemon can
        update its hit_count.
    tier_grant : Grant or None
        PR-4.1 — optional active tier grant for this peer. When present
        and well-formed, ``tier_grant.target_tier`` overrides
        ``peer_tier`` for the soft-layer decision. If the tier grant
        actually fires (the soft layer was the deciding layer), its id
        goes into ``decision.grant_hits`` so the daemon can record the
        hit and optionally mark it consumed.
    """
    reasons: list[str] = []
    grant_hits: list[str] = []
    raw_attaches: list = list(msg.get("attaches") or [])
    body = msg.get("body") or ""

    # Build (pattern → grant_id) for hit attribution. If both legacy
    # grant_paths and structured path_grants are provided, path_grants
    # wins (the structured form carries grant ids; the flat list does
    # not).
    pattern_to_grant_id: dict[str, str] = {}
    derived_grant_paths: list[str] = []
    if path_grants:
        for g in path_grants:
            for pat in getattr(g, "paths", []) or []:
                derived_grant_paths.append(pat)
                # First grant to register a pattern owns it; this only
                # matters when two grants happen to share a pattern.
                pattern_to_grant_id.setdefault(pat, getattr(g, "id", ""))
        effective_grant_paths = derived_grant_paths
    else:
        effective_grant_paths = list(grant_paths or [])

    # --- Pre-pass: normalize every attach. An attach that fails
    # normalization (absolute path with no tail, traversal segments,
    # non-string) is treated as a deliberate exfiltration attempt and
    # short-circuits to ``denied`` — no honest workflow needs ``..``.
    normalized: list[str] = []
    for raw in raw_attaches:
        norm = normalize_attach(raw)
        if norm is None:
            reasons.append(
                f"attach {raw!r} failed normalization (absolute path, "
                f"traversal, or non-string) → denied"
            )
            return Decision(action="denied", tier="hard_rule", reasons=reasons)
        normalized.append(norm)

    # --- Hard layer ---------------------------------------------------------

    # 1. Hardcoded deny paths — non-overridable system locations.
    for path in normalized:
        hit = _match_any(path, HARDCODED_DENY_PATHS)
        if hit:
            reasons.append(f"attach '{path}' hits hardcoded deny '{hit}'")
            return Decision(action="denied", tier="hard_rule", reasons=reasons)

    # 2. User deny paths from court.yaml.
    for path in normalized:
        hit = _match_any(path, deny_paths)
        if hit:
            reasons.append(f"attach '{path}' hits deny rule '{hit}'")
            return Decision(action="denied", tier="hard_rule", reasons=reasons)

    # 3. User allow paths: if specified, every attach must match one.
    # Grants (PR-4) widen this list for the duration of the grant — they
    # cannot turn a permissive (empty) allow_paths into a restrictive one,
    # but they can let a specific path through an existing restriction.
    if allow_paths and normalized:
        effective_allow = list(allow_paths) + effective_grant_paths
        for path in normalized:
            hit = _match_any(path, effective_allow)
            if hit is None:
                reasons.append(
                    f"attach '{path}' not covered by allow_paths "
                    f"{allow_paths} (no active grant either) → forcing human_required"
                )
                return Decision(
                    action="human_required", tier="hard_rule", reasons=reasons,
                    grant_hits=grant_hits,
                )
            # If the match came from a grant (not the static list) note
            # it in the audit trail AND register the grant's id for hit
            # tracking so ``banling info`` shows usage.
            if hit in effective_grant_paths and hit not in allow_paths:
                reasons.append(
                    f"attach '{path}' covered by active grant pattern '{hit}'"
                )
                gid = pattern_to_grant_id.get(hit)
                if gid and gid not in grant_hits:
                    grant_hits.append(gid)

    # 4. Sensitive keywords (hardcoded + policy extras).
    all_keywords = list(HARDCODED_KEYWORDS) + list(policy.extra_keywords)
    body_lower = body.lower()
    for kw in all_keywords:
        if kw and kw.lower() in body_lower:
            reasons.append(f"sensitive keyword '{kw}' in body → human_required")
            return Decision(
                action="human_required", tier="hard_rule", reasons=reasons,
            )

    # --- Soft layer (tier-based) -------------------------------------------

    base_tier = peer_tier or policy.default_tier
    tier = base_tier

    # Tier grant overrides peer_tier *only* on the soft layer, and only
    # if it would actually permit something more than the base tier
    # (we never silently *downgrade* via a grant — that would be
    # surprising, and there's no legitimate workflow for it).
    if tier_grant is not None:
        target = getattr(tier_grant, "target_tier", None)
        gid = getattr(tier_grant, "id", None)
        if target in _TIER_ACTION:
            base_pri = _TIER_PRIORITY.get(base_tier, -1)
            target_pri = _TIER_PRIORITY.get(target, -1)
            if target_pri > base_pri:
                reasons.append(
                    f"tier grant active: {base_tier} → {target} "
                    f"(grant_id={gid})"
                )
                tier = target
                if gid and gid not in grant_hits:
                    grant_hits.append(gid)

    action = _TIER_ACTION.get(tier, "human_required")
    if tier not in _TIER_ACTION:
        reasons.append(f"unknown tier '{tier}' → falling back to human_required")
    reasons.append(f"tier={tier} → action={action}")
    return Decision(action=action, tier=tier, reasons=reasons, grant_hits=grant_hits)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _policy_log_path(project: str) -> Path:
    from bangjiao import project_logs_dir
    return project_logs_dir(project) / "policy-log.jsonl"


def log_decision(project: str, msg: dict, decision: Decision) -> Path:
    """Append one JSON line to ``logs/policy-log.jsonl``. Returns path."""
    from bangjiao import iso_now, project_logs_dir

    project_logs_dir(project).mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": iso_now(),
        "from_court": msg.get("from_court"),
        "id": msg.get("id"),
        "from": msg.get("from"),
        "to": msg.get("to"),
        "attaches": msg.get("attaches") or [],
        "action": decision.action,
        "tier": decision.tier,
        "reasons": decision.reasons,
    }
    log_path = _policy_log_path(project)
    with log_path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# Subdir routing helper
# ---------------------------------------------------------------------------

# Where each action lands on disk, relative to bus/<from_court>/.
ACTION_SUBDIR: dict[str, str] = {
    "auto_pass": "inbox",
    "judge": "inbox",            # PR-2 stub passes through; PR-3 will refine
    "human_required": "pending-approval",
    "denied": "denied",
}


def subdir_for(action: str) -> str:
    return ACTION_SUBDIR.get(action, "denied")
