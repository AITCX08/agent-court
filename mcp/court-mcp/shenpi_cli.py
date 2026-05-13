"""agent-court — ``pizhun`` command-line entry point (PR-5).

Subcommands:

.. code-block:: shell

    court-approve <project> list                          # show pending items
    court-approve <project> approve <msg-id>              # release to inbox/
    court-approve <project> deny <msg-id>                 # park in denied/
    court-approve <project> cleanup                       # auto-deny anything past timeout_seconds

Lives next to the MCP server because they share a venv. The
``bin/court-approve`` shell wrapper just exec's the venv's python against this
module.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bangjiao  # noqa: E402
import shenpi    # noqa: E402


SUBCMDS = ("list", "approve", "deny", "cleanup")


def _resolve_issuer() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    host = os.environ.get("HOSTNAME") or ""
    return f"{user}@{host}" if host else user


def _shenpi_cfg(project: str):
    try:
        return bangjiao.load_bangjiao(project).shenpi
    except Exception:
        return None


def _cmd_list(args) -> int:
    cfg = _shenpi_cfg(args.project)
    timeout = cfg.timeout_seconds if cfg else 0
    try:
        listing = shenpi.list_pending(args.project, timeout_seconds=timeout)
    except ValueError as e:
        print(f"[court-approve] {e}", file=sys.stderr)
        return 2

    pending = listing["pending"]
    expired = listing["expired"]
    if not pending and not expired:
        print(f"[court-approve] no pending items for project '{args.project}'")
        return 0

    print(f"{'STATE':<8} {'ID':<10} {'PEER':<28} {'AGE':<8} REASONS")
    now_unix = int(datetime.now(timezone.utc).timestamp())
    for item in pending:
        age = now_unix - item.ts_unix
        reasons = item.reasons[0] if item.reasons else ""
        peer = item.peer[:26] + "…" if len(item.peer) > 28 else item.peer
        print(f"{'pending':<8} {item.msg_id:<10} {peer:<28} {_fmt_age(age):<8} {reasons[:80]}")
    for item in expired:
        age = now_unix - item.ts_unix
        peer = item.peer[:26] + "…" if len(item.peer) > 28 else item.peer
        reasons = item.reasons[0] if item.reasons else ""
        print(f"{'expired':<8} {item.msg_id:<10} {peer:<28} {_fmt_age(age):<8} {reasons[:80]}")
    return 0


def _fmt_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _cmd_approve(args) -> int:
    cfg = _shenpi_cfg(args.project)
    timeout = cfg.timeout_seconds if cfg else 0
    by = args.by or _resolve_issuer()
    try:
        result = shenpi.approve(
            args.project, args.msg_id, by=by, timeout_seconds=timeout,
        )
    except ValueError as e:
        print(f"[court-approve] {e}", file=sys.stderr)
        return 2

    if result == "approved":
        print(f"[court-approve] approved {args.msg_id} → bus/<peer>/inbox/")
        return 0
    if result == "not_found":
        print(f"[court-approve] no such pending item: {args.msg_id}", file=sys.stderr)
        return 1
    if result == "expired":
        print(
            f"[court-approve] {args.msg_id} is past timeout_seconds; run "
            f"'court-approve {args.project} deny {args.msg_id}' or 'court-approve {args.project} cleanup'.",
            file=sys.stderr,
        )
        return 1
    # io_error
    print(f"[court-approve] failed to move {args.msg_id}; check logs/approval-log.jsonl",
          file=sys.stderr)
    return 3


def _cmd_deny(args) -> int:
    by = args.by or _resolve_issuer()
    try:
        result = shenpi.deny(args.project, args.msg_id, by=by)
    except ValueError as e:
        print(f"[court-approve] {e}", file=sys.stderr)
        return 2

    if result == "denied":
        print(f"[court-approve] denied {args.msg_id} → bus/<peer>/denied/")
        return 0
    if result == "not_found":
        print(f"[court-approve] no such pending item: {args.msg_id}", file=sys.stderr)
        return 1
    # io_error
    print(f"[court-approve] failed to move {args.msg_id}; check logs/approval-log.jsonl",
          file=sys.stderr)
    return 3


def _cmd_cleanup(args) -> int:
    cfg = _shenpi_cfg(args.project)
    if cfg is None or cfg.timeout_seconds <= 0:
        print(f"[court-approve] cleanup is a no-op when timeout_seconds is 0 or unset")
        return 0
    by = args.by or _resolve_issuer()
    try:
        result = shenpi.sweep_expired(
            args.project, timeout_seconds=cfg.timeout_seconds, by=by,
        )
    except ValueError as e:
        print(f"[court-approve] {e}", file=sys.stderr)
        return 2
    swept = result.get("swept", [])
    if not swept:
        print(f"[court-approve] no expired items in '{args.project}'")
    else:
        for mid in swept:
            print(f"[court-approve] auto-denied {mid} (over timeout)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="court-approve",
        description="Approve, deny, or list留中 (pending) messages awaiting human review.",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    lst = sub.add_parser("list", help="Show pending + expired items.")
    lst.add_argument("project")
    lst.set_defaults(func=_cmd_list)

    ap = sub.add_parser("approve", help="Release a pending message to its peer inbox.")
    ap.add_argument("project")
    ap.add_argument("msg_id")
    ap.add_argument("--by", default="", help="Free-form actor tag for the audit log.")
    ap.set_defaults(func=_cmd_approve)

    dn = sub.add_parser("deny", help="Move a pending message into the denied bin.")
    dn.add_argument("project")
    dn.add_argument("msg_id")
    dn.add_argument("--by", default="", help="Free-form actor tag for the audit log.")
    dn.set_defaults(func=_cmd_deny)

    cl = sub.add_parser("cleanup",
                        help="Auto-deny everything past bangjiao.shenpi.timeout_seconds.")
    cl.add_argument("project")
    cl.add_argument("--by", default="system", help="Audit-log actor tag (default: system).")
    cl.set_defaults(func=_cmd_cleanup)

    return p


def _reorder_argv(argv: list[str]) -> list[str]:
    """Same trick as lingpai_cli: caller types ``<project> <subcmd>``,
    argparse wants subcmd first."""
    if not argv:
        return argv
    if argv[0] in ("-h", "--help"):
        return argv
    positional_idx = [i for i, t in enumerate(argv) if not t.startswith("-")]
    if not positional_idx:
        return argv
    proj_i = positional_idx[0]
    project = argv[proj_i]
    pre = argv[:proj_i]
    after = argv[proj_i + 1:]
    if not positional_idx[1:]:
        return pre + ["list", project] + after
    second_idx_in_after = positional_idx[1] - proj_i - 1
    second = after[second_idx_in_after]
    if second in SUBCMDS:
        new_after = after[:second_idx_in_after] + after[second_idx_in_after + 1:]
        return pre + [second, project] + new_after
    # Unknown token where subcmd should be — let argparse complain.
    return pre + after


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    final = _reorder_argv(argv)
    args = parser.parse_args(final)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
