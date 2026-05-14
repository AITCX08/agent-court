"""agent-court — shared peer-network primitives.

Used by ``court-keygen``, ``court-peer`` (HTTP receiver) and the MCP
server's peer tools. Lives next to the MCP server because they share a
venv (cryptography, aiohttp, pyyaml).

Identity model is **per-project** (see ARCHITECTURE.md): each project
has its own keypair + peers.yaml under
``$COURT_ROOT/projects/<project>/`` so peer ``A`` of project ``work`` has
no way to know that project ``personal`` exists on the same machine.

Functions are pure: no global mutable state. ``COURT_ROOT`` is resolved
on each call via :func:`court_root` so the module is safe to import from
tests with monkeypatched env.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
import math
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# ---------------------------------------------------------------------------
# Input-sanitization helpers (used by inbox handler + bus writer)
# ---------------------------------------------------------------------------

# court_id / role names / message ids that show up as path components on disk
# must be tightly constrained — they originate from an authenticated peer
# but a malicious-yet-registered peer could otherwise pick a value like
# "../shared" and escape ``bus/<peer>/``.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class UnsafeNameError(ValueError):
    """Raised when a peer-controlled string is not safe to use as a filesystem
    path component. Caller is expected to reject the inbound request."""


def assert_safe_path_component(value, *, field_name: str) -> str:
    """Return ``value`` unchanged if it is a single safe name, else raise.

    A "safe name" is 1–128 chars of ``[A-Za-z0-9._-]``, excluding ``.`` and
    ``..``. These rules apply to ``court_id``, role names, and message ids
    — anything that ends up as a directory or filename segment under
    ``bus/``.
    """
    if not isinstance(value, str):
        raise UnsafeNameError(f"{field_name!r} must be a string, got {type(value).__name__}")
    if value in (".", ".."):
        raise UnsafeNameError(f"{field_name!r} cannot be '.' or '..'")
    if not _SAFE_NAME.match(value):
        raise UnsafeNameError(
            f"{field_name!r}={value!r} contains characters outside "
            f"[A-Za-z0-9._-] or exceeds 128 chars"
        )
    return value


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

class ReplayCache:
    """In-memory ``id`` cache to reject duplicate inbound messages.

    Bounded size + TTL so it never grows unboundedly. Restarts lose state,
    which is fine because the ``ts`` freshness window below makes a stored
    message un-replayable past that window anyway — restart only widens
    the gap to that window.
    """

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 10_000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: dict[str, float] = {}

    def check_and_add(self, msg_id: str) -> bool:
        """Return True if this id is fresh; False if it has been seen."""
        now = time.monotonic()
        # Prune expired entries opportunistically.
        if self._seen:
            cutoff = now
            stale = [k for k, exp in self._seen.items() if exp <= cutoff]
            for k in stale:
                del self._seen[k]
        if msg_id in self._seen:
            return False
        # Cap size: evict an arbitrary entry (dict iteration order = insertion).
        if len(self._seen) >= self._max:
            try:
                oldest_key = next(iter(self._seen))
                del self._seen[oldest_key]
            except StopIteration:
                pass
        self._seen[msg_id] = now + self._ttl
        return True


def ts_is_fresh(iso_ts, *, window_seconds: int = 300) -> bool:
    """Return True if the message's ``ts`` field is within ±``window`` of now.

    Rejects non-strings, unparseable strings, and naive timestamps without
    timezone info (we don't know which clock they're on). The window is
    symmetric — a clock 4 minutes ahead is still accepted.
    """
    if not isinstance(iso_ts, str):
        return False
    try:
        msg_time = datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    if msg_time.tzinfo is None:
        return False
    now = datetime.now(timezone.utc)
    delta = abs((now - msg_time).total_seconds())
    return delta <= window_seconds


# ---------------------------------------------------------------------------
# Paths (all project-scoped)
# ---------------------------------------------------------------------------

def court_root() -> Path:
    return Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court")))


def project_dir(project: str) -> Path:
    return court_root() / "projects" / project


def project_bus_dir(project: str) -> Path:
    return project_dir(project) / "bus"


def project_identity_dir(project: str) -> Path:
    return project_dir(project) / "identity"


def project_priv_key_path(project: str) -> Path:
    return project_identity_dir(project) / "priv.key"


def project_pub_key_path(project: str) -> Path:
    return project_identity_dir(project) / "pub.key"


def project_peers_yaml_path(project: str) -> Path:
    return project_dir(project) / "peers.yaml"


def project_court_yaml_path(project: str) -> Path:
    return project_dir(project) / "court.yaml"


def project_logs_dir(project: str) -> Path:
    return project_dir(project) / "logs"


def project_peer_errors_log(project: str) -> Path:
    return project_logs_dir(project) / "peer-errors.log"


def all_projects() -> list[str]:
    base = court_root() / "projects"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "court.yaml").is_file())


# ---------------------------------------------------------------------------
# Keypair
# ---------------------------------------------------------------------------

@dataclass
class Identity:
    project: str
    priv: Ed25519PrivateKey
    pub: Ed25519PublicKey
    pub_b64: str
    fingerprint: str


def fingerprint_from_pub_b64(pub_b64: str) -> str:
    """SHA-256 of raw public key bytes, first 16 bytes hex."""
    raw = base64.b64decode(pub_b64)
    digest = hashlib.sha256(raw).digest()
    return digest[:16].hex()


def generate_keypair(project: str, *, force: bool = False) -> Identity:
    """Generate a new ed25519 keypair for ``project``.

    Returns the new identity. Raises ``FileExistsError`` if a key already
    exists and ``force`` is False.
    """
    project_identity_dir(project).mkdir(parents=True, exist_ok=True)
    priv_path = project_priv_key_path(project)
    pub_path = project_pub_key_path(project)
    if priv_path.exists() and not force:
        raise FileExistsError(
            f"keypair already exists at {priv_path}; pass force=True to overwrite"
        )

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    from cryptography.hazmat.primitives import serialization

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    priv_b64 = base64.b64encode(priv_bytes).decode()
    pub_b64 = base64.b64encode(pub_bytes).decode()

    priv_path.write_text(priv_b64 + "\n")
    os.chmod(priv_path, 0o600)
    pub_path.write_text(pub_b64 + "\n")
    os.chmod(pub_path, 0o644)

    return Identity(
        project=project,
        priv=priv,
        pub=pub,
        pub_b64=pub_b64,
        fingerprint=fingerprint_from_pub_b64(pub_b64),
    )


def load_identity(project: str) -> Identity:
    """Load the project's identity. Raises FileNotFoundError if absent."""
    priv_path = project_priv_key_path(project)
    pub_path = project_pub_key_path(project)
    if not priv_path.exists():
        raise FileNotFoundError(
            f"no keypair at {priv_path} — run `court-keygen {project}` first"
        )
    priv_b64 = priv_path.read_text().strip()
    pub_b64 = pub_path.read_text().strip()

    priv_bytes = base64.b64decode(priv_b64)
    pub_bytes = base64.b64decode(pub_b64)
    priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
    return Identity(
        project=project,
        priv=priv,
        pub=pub,
        pub_b64=pub_b64,
        fingerprint=fingerprint_from_pub_b64(pub_b64),
    )


# ---------------------------------------------------------------------------
# Canonical JSON + signing
# ---------------------------------------------------------------------------

SIGNED_FIELDS: tuple[str, ...] = (
    "attaches",        # PR-2: explicit file/path references, must be signed so a peer
                       # can't strip or forge them after the sender signed the message
    "body",
    "from",
    "from_court",
    "id",
    "in_reply_to",
    "to",
    "ts",
)


def canonical_payload(msg: dict) -> bytes:
    """Pick the fields covered by the signature and JSON-dump them deterministically."""
    payload = {k: msg[k] for k in SIGNED_FIELDS if k in msg and msg[k] is not None}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_message(msg: dict, priv: Ed25519PrivateKey) -> str:
    sig = priv.sign(canonical_payload(msg))
    return base64.b64encode(sig).decode()


def verify_signature(msg: dict, signature_b64, sender_pub_b64) -> bool:
    """Verify ``signature_b64`` against the canonical payload of ``msg``.

    Returns False (never raises) for *any* failure: bad base64, wrong key
    length, signature mismatch, non-string inputs. The caller treats False
    as "401 bad signature" — we deliberately don't distinguish "malformed
    sig" from "wrong key" since both mean the same thing to the peer.
    """
    if not isinstance(signature_b64, str) or not isinstance(sender_pub_b64, str):
        return False
    try:
        sig_bytes = base64.b64decode(signature_b64, validate=True)
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(sender_pub_b64, validate=True))
        pub.verify(sig_bytes, canonical_payload(msg))
        return True
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# court.yaml federation block
# ---------------------------------------------------------------------------

@dataclass
class TuiguanConfig:
    """PR-3 — config for the LLM judge invoked on policy `judge` decisions.

    All fields are optional. ``cli``/``model``/``prompt_file`` = None means
    "fall back to a sensible default": ``cli`` falls back to court.yaml's
    top-level ``default_cli``; ``model`` is left as the CLI's own default;
    ``prompt_file`` falls back to the built-in
    ``mcp/court-mcp/prompts/judge.md``.
    """
    cli: Optional[str] = None
    model: Optional[str] = None
    prompt_file: Optional[str] = None
    timeout_seconds: float = 30.0
    confidence_threshold: float = 0.6


@dataclass
class FeishuChannelConfig:
    """PR-5 — config for the feishu (Lark) webhook notification channel."""
    webhook_url: Optional[str] = None
    # Mention IDs / open-ids to append to the message body. The Feishu bot
    # API turns ``@<open-id>`` into a real mention when the bot is in the
    # same chat as the recipient; harmless when it isn't.
    mention: list[str] = field(default_factory=list)
    # PR-6 — per-channel override of approvals.max_retries. None falls
    # back to the global default.
    max_retries: Optional[int] = None


@dataclass
class WechatChannelConfig:
    """PR-5 — config for outbound notification via the cc-connect bridge.

    ``cc_connect_project`` and ``cc_connect_session_key`` together name the
    WeChat conversation cc-connect should push the notification into. They
    map onto cc-connect's own ``CC_PROJECT`` + ``CC_SESSION_KEY`` env vars
    (the same pair claude-inside-cc-connect uses to address ``cc-connect
    send``).
    """
    cc_connect_bin: str = "cc-connect"          # binary on PATH
    cc_connect_project: Optional[str] = None    # CC_PROJECT
    cc_connect_session_key: Optional[str] = None  # CC_SESSION_KEY
    # PR-6 — per-channel override of approvals.max_retries.
    max_retries: Optional[int] = None


@dataclass
class ShenpiConfig:
    """PR-5 — config for human-approval notification + timeout sweep.

    Default disabled; turning it on adds one extra step per inbound
    ``human_required`` decision (fire-and-forget notify dispatch to each
    enabled channel).

    PR-6 added the ``delivery_policy`` + retry knobs:

    - ``delivery_policy = "broadcast"`` (default): fire every enabled
      channel concurrently. Each channel retries independently on
      transient failure. Useful when you actively *want* to get
      pinged on multiple devices.
    - ``delivery_policy = "escalate"``: walk ``channels`` in order;
      stop on the first channel that delivers successfully. Each
      channel exhausts its retries before we fall through to the next.
      Useful when the channels are ranked by preference (e.g. Feishu
      first, fallback to WeChat if Feishu's webhook is down).

    ``max_retries`` (default 0 = no retry, same as PR-5) and
    ``backoff_seconds`` (initial delay; doubles each retry attempt,
    exponential) shape the per-channel retry loop. A channel may
    override ``max_retries`` in its own block.
    """
    enabled: bool = False
    channels: list[str] = field(default_factory=list)   # subset of {terminal, feishu, wechat}
    timeout_seconds: int = 0                            # 0 = no timeout
    delivery_policy: str = "broadcast"                  # "broadcast" | "escalate"
    max_retries: int = 0                                # global default
    backoff_seconds: float = 3.0                        # initial backoff, doubles each retry
    feishu: FeishuChannelConfig = field(default_factory=FeishuChannelConfig)
    wechat: WechatChannelConfig = field(default_factory=WechatChannelConfig)


@dataclass
class BangjiaoConfig:
    enabled: bool = False
    court_id: str = ""
    # Roles outside peers may dispatch *to*. Missing in YAML → fail-closed
    # to ``["foreman"]`` so a misconfigured court doesn't accidentally
    # accept inbound dispatches to every role. An explicit empty list
    # means "expose nothing" and locks the daemon down completely.
    expose_roles: list[str] = field(default_factory=lambda: ["foreman"])
    allow_paths: list[str] = field(default_factory=list)         # glob whitelist (PR-2 enforces)
    deny_paths: list[str] = field(default_factory=list)          # glob blacklist (PR-2 enforces)
    tuiguan: TuiguanConfig = field(default_factory=TuiguanConfig)      # PR-3 LLM judge config
    shenpi: ShenpiConfig = field(default_factory=ShenpiConfig)         # PR-5 human-approval channels
    default_cli: str = "claude"                                   # court.yaml top-level — used when tuiguan.cli is unset


def _default_court_id(project: str) -> str:
    host = os.environ.get("COURT_HOSTNAME") or socket.gethostname() or "host"
    # strip the trailing ".local" macOS adds, looks ugly in network configs
    host = host.removesuffix(".local")
    return f"{host}-{project}"


def load_bangjiao(project: str) -> BangjiaoConfig:
    """Read court.yaml's ``bangjiao:`` block.

    Returns a disabled config if the file is missing, the block is absent,
    or ``enabled: false``. court_id falls back to ``<hostname>-<project>``.
    """
    cfg_path = project_court_yaml_path(project)
    raw = {}
    if cfg_path.is_file():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}

    # ``federation:`` is canonical; ``bangjiao:`` is the brief alias from
    # the PR-2 rename and is kept for backward compat so anyone who edited
    # their court.yaml in that window keeps working.
    block = raw.get("federation") or raw.get("bangjiao") or {}
    enabled = bool(block.get("enabled", False))
    tuiguan_cfg = _parse_tuiguan_config(block.get("judge") or {})

    # expose_roles defaulting:
    # - key missing from YAML → fail-closed to ["foreman"]
    # - explicit empty list   → respect the user; no role exposed (locks down)
    raw_expose = block.get("expose_roles")
    if raw_expose is None:
        expose_roles = ["foreman"]
    else:
        expose_roles = [str(r) for r in raw_expose]

    shenpi_cfg = _parse_shenpi_config(block.get("approvals") or block.get("shenpi") or {})

    return BangjiaoConfig(
        enabled=enabled,
        court_id=block.get("court_id") or _default_court_id(project),
        expose_roles=expose_roles,
        allow_paths=list(block.get("allow_paths") or []),
        deny_paths=list(block.get("deny_paths") or []),
        tuiguan=tuiguan_cfg,
        shenpi=shenpi_cfg,
        default_cli=str(raw.get("default_cli") or "claude"),
    )


_VALID_SHENPI_CHANNELS = ("terminal", "feishu", "wechat")


def _clamp_optional_retries(raw) -> Optional[int]:
    """Per-channel ``max_retries`` override accepts ``None`` (= use the
    global default), ``int >= 0``, or anything malformed (clamped to None
    so the global default takes over)."""
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return max(n, 0)


def _parse_shenpi_config(block: dict) -> ShenpiConfig:
    """Build a ShenpiConfig from the raw YAML dict, clamping invalid values
    to safe defaults so a misconfigured ``shenpi:`` block can't silently
    disable approval routing."""
    enabled = bool(block.get("enabled", False))

    # Channels: keep order, drop unknowns. ``terminal`` is implied when the
    # block enables shenpi without listing channels (so a typo doesn't
    # silently mute notifications).
    raw_channels = block.get("channels")
    if raw_channels is None:
        channels = ["terminal"] if enabled else []
    else:
        channels = []
        for c in raw_channels:
            c_str = str(c).lower()
            if c_str in _VALID_SHENPI_CHANNELS and c_str not in channels:
                channels.append(c_str)

    # Timeout: int seconds; <= 0 means "no timeout".
    raw_timeout = block.get("timeout_seconds", 0)
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = 0
    if timeout < 0:
        timeout = 0

    # PR-6 — delivery policy. Anything other than "escalate" collapses to
    # "broadcast" so a typo still preserves the PR-5 behaviour (fan-out).
    raw_policy = str(block.get("delivery_policy") or "broadcast").lower()
    delivery_policy = "escalate" if raw_policy == "escalate" else "broadcast"

    # PR-6 — global retry knobs. Clamp out negatives + NaN so the retry
    # loop can't run forever.
    raw_retries = block.get("max_retries", 0)
    try:
        max_retries = int(raw_retries)
    except (TypeError, ValueError):
        max_retries = 0
    if max_retries < 0:
        max_retries = 0

    raw_backoff = block.get("backoff_seconds", 3.0)
    try:
        backoff = float(raw_backoff)
    except (TypeError, ValueError):
        backoff = 3.0
    if not math.isfinite(backoff) or backoff < 0:
        backoff = 3.0

    feishu_block = block.get("feishu") or {}
    feishu = FeishuChannelConfig(
        webhook_url=feishu_block.get("webhook_url") or None,
        mention=[str(m) for m in (feishu_block.get("mention") or [])],
        max_retries=_clamp_optional_retries(feishu_block.get("max_retries")),
    )

    wechat_block = block.get("wechat") or {}
    wechat = WechatChannelConfig(
        cc_connect_bin=str(wechat_block.get("cc_connect_bin") or "cc-connect"),
        cc_connect_project=(
            str(wechat_block["cc_connect_project"])
            if wechat_block.get("cc_connect_project") else None
        ),
        cc_connect_session_key=(
            str(wechat_block["cc_connect_session_key"])
            if wechat_block.get("cc_connect_session_key") else None
        ),
        max_retries=_clamp_optional_retries(wechat_block.get("max_retries")),
    )

    return ShenpiConfig(
        enabled=enabled,
        channels=channels,
        timeout_seconds=timeout,
        delivery_policy=delivery_policy,
        max_retries=max_retries,
        backoff_seconds=backoff,
        feishu=feishu,
        wechat=wechat,
    )


def _parse_tuiguan_config(tuiguan_block: dict) -> TuiguanConfig:
    """Build a TuiguanConfig from the raw YAML dict, clamping invalid values
    to safe defaults so a misconfigured judge can't silently break the
    fail-safe escape hatch."""
    raw_timeout = tuiguan_block.get("timeout_seconds", 30)
    try:
        timeout = float(raw_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 30.0
    except (TypeError, ValueError):
        timeout = 30.0

    raw_threshold = tuiguan_block.get("confidence_threshold", 0.6)
    try:
        threshold = float(raw_threshold)
        if not math.isfinite(threshold):
            threshold = 0.6
        else:
            # Clamp to [0, 1]; values outside this range don't have a
            # well-defined meaning for the upgrade check.
            threshold = max(0.0, min(1.0, threshold))
    except (TypeError, ValueError):
        threshold = 0.6

    return TuiguanConfig(
        cli=tuiguan_block.get("cli"),
        model=tuiguan_block.get("model"),
        prompt_file=tuiguan_block.get("prompt_file"),
        timeout_seconds=timeout,
        confidence_threshold=threshold,
    )


# ---------------------------------------------------------------------------
# peers.yaml
# ---------------------------------------------------------------------------

@dataclass
class Peer:
    name: str
    court_id: str
    url: str
    pub_key_fingerprint: str
    pub_key_b64: Optional[str]
    relation: str   # parent | child | sibling (was "role" pre-PR-1; renamed to disambiguate from agent roles)
    policy_tier: Optional[str] = None   # PR-2: tier_a | tier_b | tier_c. None → fall through to policy.default_tier


@dataclass
class PeersConfig:
    project: str
    self_court_id: str
    self_fingerprint: str
    peers: list[Peer]

    def by_court_id(self, court_id: str) -> Optional[Peer]:
        for p in self.peers:
            if p.court_id == court_id:
                return p
        return None


def load_peers(project: str) -> PeersConfig:
    """Load this project's peers.yaml + reconcile with the federation block & key on disk."""
    fed = load_bangjiao(project)

    p = project_peers_yaml_path(project)
    raw = {}
    if p.is_file():
        with p.open() as f:
            raw = yaml.safe_load(f) or {}

    self_block = raw.get("self") or {}
    self_court_id = self_block.get("court_id") or fed.court_id

    try:
        identity = load_identity(project)
        self_fp = identity.fingerprint
    except FileNotFoundError:
        self_fp = self_block.get("pub_key_fingerprint", "")

    peers = []
    for entry in raw.get("peers") or []:
        peers.append(Peer(
            name=entry.get("name", entry.get("court_id", "")),
            court_id=entry["court_id"],
            url=entry["url"].rstrip("/"),
            pub_key_fingerprint=entry["pub_key_fingerprint"],
            pub_key_b64=entry.get("pub_key_b64"),
            # Accept the historical "role" key as a fallback so a stale config
            # doesn't lock anyone out.
            relation=entry.get("relation") or entry.get("role") or "sibling",
            policy_tier=entry.get("policy_tier"),
        ))
    return PeersConfig(
        project=project,
        self_court_id=self_court_id,
        self_fingerprint=self_fp,
        peers=peers,
    )


# ---------------------------------------------------------------------------
# Path glob helpers (PR-2 will call these; defined now so the schema is wired)
# ---------------------------------------------------------------------------

def path_allowed(candidate: str, allow: list[str], deny: list[str]) -> bool:
    """Decide whether ``candidate`` (an absolute or repo-relative path) is reachable.

    Rules:
    - deny wins: if any deny glob matches, return False
    - if allow is empty, any non-denied path passes
    - if allow has entries, candidate must match at least one
    """
    for pattern in deny:
        if fnmatch.fnmatch(candidate, pattern):
            return False
    if not allow:
        return True
    return any(fnmatch.fnmatch(candidate, p) for p in allow)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_peer_error(project: str, line: str) -> None:
    project_logs_dir(project).mkdir(parents=True, exist_ok=True)
    with project_peer_errors_log(project).open("a") as f:
        f.write(f"[{iso_now()}] {line}\n")


# ---------------------------------------------------------------------------
# Bus file emission
# ---------------------------------------------------------------------------

def write_inbound_to_bus(
    project: str,
    msg: dict,
    *,
    subdir: str = "inbox",
    policy_decision: Optional[str] = None,
    policy_reasons: Optional[list[str]] = None,
) -> Path:
    """Write a verified inbound peer message into the project's bus.

    Default lands at ``$bus/<from_court>/inbox/<unix_ts>-<id>-<from>-to-<to>.md``.
    PR-2 callers may pass ``subdir="pending-approval"`` or ``"denied"`` to
    park messages that didn't auto-pass; the foreman never sees those
    files unless a human moves them into ``inbox/``.

    When ``policy_decision`` is given, frontmatter gets two extra fields
    (``policy_decision``, ``policy_reasons``) so a downstream reader
    (foreman, llm_judge, human reviewer) can see *why* the message
    landed where it did without consulting the audit log.

    The existing ``court-watcher`` only inspects ``*/outbox/*.md`` files,
    so writing into ``inbox`` / ``pending-approval`` / ``denied`` here
    does not double-route through the watcher.

    Raises :class:`UnsafeNameError` if any field that becomes a path
    component (``from_court``, ``from``, ``to``, ``id``) contains
    characters outside ``[A-Za-z0-9._-]`` or is ``.`` / ``..``. Caller
    should turn that into a 400 rejection — the message has cleared
    signature + role checks so the field values are authenticated, but
    a malicious-yet-registered peer could still pick a hostile name.
    """
    from_court = assert_safe_path_component(msg["from_court"], field_name="from_court")
    msg_id = assert_safe_path_component(msg["id"], field_name="id")
    raw_sender = msg.get("from", from_court)
    sender_role = assert_safe_path_component(raw_sender, field_name="from")
    to_role = assert_safe_path_component(msg["to"], field_name="to")
    ts_epoch = int(datetime.now().timestamp())
    fname = f"{ts_epoch}-{msg_id}-{sender_role}-to-{to_role}.md"

    target = project_bus_dir(project) / from_court / subdir
    target.mkdir(parents=True, exist_ok=True)
    # Only the canonical inbox needs a .done sidecar (court-watcher pattern);
    # pending-approval / denied don't get auto-archived.
    if subdir == "inbox":
        (target / ".done").mkdir(exist_ok=True)

    fpath = target / fname
    lines = [
        "---",
        f"from: {sender_role}",
        f"from_court: {from_court}",
        f"to: {to_role}",
        f"ts: {msg.get('ts', iso_now())}",
        f"id: {msg_id}",
    ]
    in_reply_to = msg.get("in_reply_to")
    if in_reply_to:
        lines.append(f"in_reply_to: {in_reply_to}")
    attaches = msg.get("attaches") or []
    if attaches:
        lines.append(f"attaches: {json.dumps(attaches, ensure_ascii=False)}")
    if policy_decision:
        lines.append(f"policy_decision: {policy_decision}")
    if policy_reasons:
        lines.append(f"policy_reasons: {json.dumps(policy_reasons, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(msg.get("body", ""))
    fpath.write_text("\n".join(lines) + "\n")
    return fpath


def ensure_dirs(*paths: Iterable[Path]) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)
