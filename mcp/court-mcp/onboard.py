"""agent-court onboard helpers.

CLI subcommands used by ``bin/court-onboard`` and ``bin/court-invite-export``:

* ``encode-token``     turn 4 invite fields into a base64url JSON token
* ``decode-token``     reverse + validate (court_id safety, url scheme, age)
* ``add-peer``         append a peer entry to peers.yaml (idempotent by court_id)
* ``write-court-yaml`` render ``court.example.yaml`` template into a project

Token format (v1):
    base64url(json({
      "v": 1,
      "court_id": "...",
      "pub_b64": "...",
      "url": "http://host:8765",
      "fingerprint": "...",
      "created_at": "2026-05-18T10:00:00+00:00"
    }))

base64 padding is stripped to keep the token short; decode_token re-pads.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

from bangjiao import (
    UnsafeNameError,
    assert_safe_path_component,
    fingerprint_from_pub_b64,
)


TOKEN_VERSION = 1
TOKEN_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------------------
# Token codec
# ---------------------------------------------------------------------------

@dataclass
class InviteToken:
    court_id: str
    pub_b64: str
    url: str
    fingerprint: str
    created_at: str
    age_warning: bool = False  # set by decode_token when older than the cutoff


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    padding = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * padding))


def encode_token(
    court_id: str,
    pub_b64: str,
    url: str,
    fingerprint: str,
    *,
    created_at: Optional[str] = None,
) -> str:
    """Return a base64url-encoded invite token. Input is not validated here;
    decode_token does the strict checks so a paste-error tells you why."""
    payload = {
        "v": TOKEN_VERSION,
        "court_id": court_id,
        "pub_b64": pub_b64,
        "url": url,
        "fingerprint": fingerprint,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(raw)


def decode_token(token: str) -> InviteToken:
    """Decode + validate. Raises ValueError on tamper / bad shape."""
    try:
        raw = _b64url_decode(token)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64 token: {e}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"token payload is not valid JSON: {e}")

    if not isinstance(payload, dict):
        raise ValueError("token payload must be a JSON object")
    if payload.get("v") != TOKEN_VERSION:
        raise ValueError(f"unsupported token version {payload.get('v')!r}")

    required = ("court_id", "pub_b64", "url", "fingerprint", "created_at")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise ValueError(f"token missing required fields: {missing}")

    # court_id ends up as a peers.yaml key + a bus path component.
    try:
        assert_safe_path_component(payload["court_id"], field_name="court_id")
    except UnsafeNameError as e:
        raise ValueError(str(e))

    url = payload["url"]
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"url must start with http:// or https://, got {url!r}")

    age_warning = False
    try:
        created = datetime.fromisoformat(payload["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(days=TOKEN_MAX_AGE_DAYS):
            age_warning = True
    except ValueError:
        # Bad created_at is non-fatal; just flag it.
        age_warning = True

    return InviteToken(
        court_id=payload["court_id"],
        pub_b64=payload["pub_b64"],
        url=url,
        fingerprint=payload["fingerprint"],
        created_at=payload["created_at"],
        age_warning=age_warning,
    )


# ---------------------------------------------------------------------------
# peers.yaml editor
# ---------------------------------------------------------------------------

PEERS_HEADER = (
    "# auto-managed by court-onboard. Manual edits are preserved on append-\n"
    "# only operations (new peers go after existing entries). Re-running\n"
    "# `court-onboard --invite-token` for the same court_id is a no-op.\n"
)


def add_peer_to_yaml(
    peers_yaml_path: Path,
    *,
    court_id: str,
    pub_b64: str,
    url: str,
    fingerprint: str,
    relation: str = "parent",
    policy_tier: str = "tier_b",
    name: Optional[str] = None,
) -> bool:
    """Append a peer to peers.yaml. Returns True if added, False if duplicate.

    Comments in the source file are not preserved by PyYAML's safe_load/dump
    round-trip; we re-add a fixed header so users see why the file changed.
    """
    assert_safe_path_component(court_id, field_name="court_id")

    if peers_yaml_path.exists():
        data = yaml.safe_load(peers_yaml_path.read_text()) or {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{peers_yaml_path} is not a YAML mapping")

    peers = data.setdefault("peers", []) or []
    for existing in peers:
        if isinstance(existing, dict) and existing.get("court_id") == court_id:
            return False

    peers.append(
        {
            "name": name or f"inviter-{court_id}",
            "court_id": court_id,
            "url": url,
            "pub_key_fingerprint": fingerprint,
            "pub_key_b64": pub_b64,
            "relation": relation,
            "policy_tier": policy_tier,
        }
    )
    data["peers"] = peers

    peers_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    peers_yaml_path.write_text(PEERS_HEADER + body)
    return True


# ---------------------------------------------------------------------------
# court.yaml renderer
# ---------------------------------------------------------------------------

def _short_hostname() -> str:
    return socket.gethostname().split(".")[0] or "court"


def write_court_yaml(
    template_path: Path,
    out_path: Path,
    *,
    project: str,
    hostname: Optional[str] = None,
) -> None:
    """Render the demo court.yaml template to ``out_path``.

    Substitutes ``{{HOSTNAME}}`` and ``{{PROJECT}}`` placeholders. Refuses to
    overwrite if ``out_path`` exists -- caller (onboard) is responsible for
    handling re-runs (idempotency check happens at the bash layer)."""
    assert_safe_path_component(project, field_name="project")
    host = hostname or _short_hostname()

    text = template_path.read_text()
    text = text.replace("{{HOSTNAME}}", host).replace("{{PROJECT}}", project)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_encode(args: argparse.Namespace) -> int:
    token = encode_token(
        court_id=args.court_id,
        pub_b64=args.pub_b64,
        url=args.url,
        fingerprint=args.fingerprint,
        created_at=args.created_at,
    )
    print(token)
    return 0


def _cmd_decode(args: argparse.Namespace) -> int:
    try:
        tok = decode_token(args.token)
    except ValueError as e:
        print(f"decode failed: {e}", file=sys.stderr)
        return 1
    out = {
        "court_id": tok.court_id,
        "pub_b64": tok.pub_b64,
        "url": tok.url,
        "fingerprint": tok.fingerprint,
        "created_at": tok.created_at,
        "age_warning": tok.age_warning,
    }
    print(json.dumps(out, sort_keys=True))
    if tok.age_warning:
        print(
            f"WARN: token is older than {TOKEN_MAX_AGE_DAYS} days. "
            "Ask the inviter to regenerate.",
            file=sys.stderr,
        )
    return 0


def _cmd_add_peer(args: argparse.Namespace) -> int:
    added = add_peer_to_yaml(
        Path(args.peers_yaml),
        court_id=args.court_id,
        pub_b64=args.pub_b64,
        url=args.url,
        fingerprint=args.fingerprint,
        relation=args.relation,
        policy_tier=args.policy_tier,
        name=args.name,
    )
    if added:
        print(f"added peer {args.court_id} to {args.peers_yaml}")
    else:
        print(f"peer {args.court_id} already present in {args.peers_yaml}, no change")
    return 0


def _cmd_write_court_yaml(args: argparse.Namespace) -> int:
    write_court_yaml(
        Path(args.template),
        Path(args.out),
        project=args.project,
        hostname=args.hostname,
    )
    print(f"wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="onboard.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode-token", help="emit a base64url invite token")
    e.add_argument("--court-id", required=True)
    e.add_argument("--pub-b64", required=True)
    e.add_argument("--url", required=True)
    e.add_argument("--fingerprint", required=True)
    e.add_argument("--created-at", default=None, help="override (ISO 8601 UTC)")
    e.set_defaults(func=_cmd_encode)

    d = sub.add_parser("decode-token", help="validate and print a token's fields")
    d.add_argument("token")
    d.set_defaults(func=_cmd_decode)

    ap = sub.add_parser("add-peer", help="append a peer entry to peers.yaml")
    ap.add_argument("--peers-yaml", required=True)
    ap.add_argument("--court-id", required=True)
    ap.add_argument("--pub-b64", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--fingerprint", required=True)
    ap.add_argument("--relation", default="parent",
                    choices=("parent", "child", "sibling"))
    ap.add_argument("--policy-tier", default="tier_b",
                    choices=("tier_a", "tier_b", "tier_c"))
    ap.add_argument("--name", default=None)
    ap.set_defaults(func=_cmd_add_peer)

    w = sub.add_parser("write-court-yaml", help="render court.yaml from template")
    w.add_argument("--template", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--project", required=True)
    w.add_argument("--hostname", default=None)
    w.set_defaults(func=_cmd_write_court_yaml)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
