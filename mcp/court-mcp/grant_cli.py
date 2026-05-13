"""agent-court — ``court-grant`` command-line entry point (PR-4).

Four subcommands:

.. code-block:: shell

    # Path grant (widens allow_paths)
    court-grant <project> <peer> <path> [<path>...] [--ttl 30m]

    # Tier grant (overrides peer's policy_tier)
    court-grant <project> <peer> --tier tier_c [--ttl 1h | --once]

    # Inspection
    court-grant <project> list
    court-grant <project> info <grant-id>
    court-grant <project> revoke <grant-id>

The bare three-arg form ``<project> <peer> <path>`` is treated as
``add`` so daily use stays terse.

Lives next to the MCP server because they share a venv. The
``bin/court-grant`` shell wrapper just exec's the venv's python
against this module.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Make sibling modules importable when invoked directly via venv python.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import grants  # noqa: E402
from peer_lib import project_dir  # noqa: E402


# Subcommands we recognize when the user types
# ``court-grant <project> <subcmd> ...``. Any other 2nd token is treated
# as a peer name (implicit ``add``).
SUBCMDS: tuple[str, ...] = ("add", "list", "info", "revoke")


def _resolve_issuer() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    host = os.environ.get("HOSTNAME") or ""
    if host:
        return f"{user}@{host}"
    return user


def _project_missing(project: str) -> bool:
    try:
        return not Path(project_dir(project)).is_dir()
    except Exception:
        return True


def _cmd_add(args) -> int:
    if _project_missing(args.project):
        print(
            f"[court-grant] project '{args.project}' not found",
            file=sys.stderr,
        )
        return 1

    issued_by = args.issued_by or _resolve_issuer()

    try:
        if args.tier:
            grant = grants.mint_tier_grant(
                args.project,
                args.peer,
                args.tier,
                ttl=args.ttl,
                consume_on_use=bool(args.once),
                issued_by=issued_by,
            )
        else:
            if not args.paths:
                print(
                    "[court-grant] path grants require at least one path glob; "
                    "to mint a tier grant pass --tier <tier_a|tier_b|tier_c>",
                    file=sys.stderr,
                )
                return 2
            if args.once:
                print(
                    "[court-grant] --once only applies to tier grants (use with --tier)",
                    file=sys.stderr,
                )
                return 2
            grant = grants.mint_path_grant(
                args.project,
                args.peer,
                args.paths,
                ttl=args.ttl,
                issued_by=issued_by,
            )
    except ValueError as e:
        print(f"[court-grant] {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"[court-grant] io error: {e}", file=sys.stderr)
        return 3

    print(f"grant_type    : {grant.grant_type}")
    print(f"granted to    : {grant.granted_to}")
    if grant.grant_type == "path":
        print(f"paths         : {grant.paths}")
    else:
        print(f"target_tier   : {grant.target_tier}")
        print(f"consume_on_use: {grant.consume_on_use}")
    print(f"id            : {grant.id}")
    print(f"issued_ts     : {grant.issued_ts}")
    print(f"expires_ts    : {grant.expires_ts}")
    print(f"issued_by     : {grant.issued_by}")
    print(f"file          : {grants.grants_dir(args.project) / (grant.id + '.json')}")
    return 0


def _cmd_list(args) -> int:
    if _project_missing(args.project):
        print(f"[court-grant] project '{args.project}' not found", file=sys.stderr)
        return 1
    try:
        rows = grants.list_grants(args.project)
    except ValueError as e:
        print(f"[court-grant] {e}", file=sys.stderr)
        return 2

    if not rows:
        print(f"[court-grant] no grants for project '{args.project}'")
        return 0

    print(
        f"{'STATE':<8} {'T':<1} {'ID':<10} {'PEER':<28} "
        f"{'EXPIRES':<27} {'HITS':<5} DETAIL"
    )
    for g in rows:
        if g.consumed_ts is not None:
            state = "consumed"
        elif g.is_active():
            state = "active"
        else:
            state = "expired"
        type_letter = "T" if g.grant_type == "tier" else "P"
        peer = g.granted_to
        if len(peer) > 26:
            peer = peer[:25] + "…"
        if g.grant_type == "tier":
            detail = f"→{g.target_tier}{' [once]' if g.consume_on_use else ''}"
        else:
            detail = ", ".join(g.paths)
        print(
            f"{state:<8} {type_letter:<1} {g.id:<10} {peer:<28} "
            f"{g.expires_ts:<27} {g.hit_count:<5} {detail}"
        )
    return 0


def _fmt_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "expired"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return "".join(parts) or f"{seconds}s"


def _cmd_info(args) -> int:
    if _project_missing(args.project):
        print(f"[court-grant] project '{args.project}' not found", file=sys.stderr)
        return 1
    try:
        g = grants.find_grant(args.project, args.grant_id)
    except ValueError as e:
        print(f"[court-grant] {e}", file=sys.stderr)
        return 2
    if g is None:
        print(f"[court-grant] no such grant: {args.grant_id}", file=sys.stderr)
        return 1

    if g.consumed_ts is not None:
        state = "consumed"
    elif g.is_active():
        state = "active"
    else:
        state = "expired"

    print(f"id            : {g.id}")
    print(f"grant_type    : {g.grant_type}")
    print(f"state         : {state}")
    print(f"granted_to    : {g.granted_to}")
    if g.grant_type == "path":
        print(f"paths         : {g.paths}")
    else:
        print(f"target_tier   : {g.target_tier}")
        print(f"consume_on_use: {g.consume_on_use}")
        if g.consumed_ts:
            print(f"consumed_ts   : {g.consumed_ts}")
    print(f"issued_ts     : {g.issued_ts}")
    print(f"issued_by     : {g.issued_by}")
    print(f"expires_ts    : {g.expires_ts}")
    print(f"remaining     : {_fmt_remaining(g.remaining_seconds())}")
    print(f"hit_count     : {g.hit_count}")
    if g.last_hit_ts:
        print(f"last_hit_ts   : {g.last_hit_ts}")
    print(f"file          : {grants.grants_dir(args.project) / (g.id + '.json')}")
    return 0


def _cmd_revoke(args) -> int:
    if _project_missing(args.project):
        print(f"[court-grant] project '{args.project}' not found", file=sys.stderr)
        return 1
    try:
        result = grants.revoke_grant(args.project, args.grant_id)
    except ValueError as e:
        print(f"[court-grant] {e}", file=sys.stderr)
        return 2
    if result == "revoked":
        print(f"[court-grant] revoked {args.grant_id}")
        return 0
    if result == "invalid_id":
        print(f"[court-grant] invalid grant id: {args.grant_id}", file=sys.stderr)
        return 2
    if result == "not_found":
        print(f"[court-grant] no such grant: {args.grant_id}", file=sys.stderr)
        return 1
    # io_error
    print(f"[court-grant] failed to delete {args.grant_id} (see logs/peer-errors.log)",
          file=sys.stderr)
    return 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="court-grant",
        description="Mint / list / inspect / revoke temporary grants for a federated peer.",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    add = sub.add_parser(
        "add",
        help="Mint a new grant (path grant by default; pass --tier for a tier grant).",
    )
    add.add_argument("project")
    add.add_argument("peer", help="The peer's court_id, as it appears in peers.yaml.")
    add.add_argument("paths", nargs="*",
                     help="One or more path globs (path grant). Omit when using --tier.")
    add.add_argument("--tier", default="",
                     choices=("", "tier_a", "tier_b", "tier_c"),
                     help="Mint a tier grant overriding the peer's policy tier.")
    add.add_argument("--once", action="store_true",
                     help="Tier grant only: consume after the first inbound match.")
    add.add_argument("--ttl", default="30m",
                     help="How long the grant is valid (30m, 1h, 2h30m, 1d, ...). Default 30m. Capped at 1y.")
    add.add_argument("--issued-by", default="",
                     help="Free-form issuer tag for the audit log. Default: $USER@$HOSTNAME.")
    add.set_defaults(func=_cmd_add)

    lst = sub.add_parser("list", help="List all grants (active + expired) for a project.")
    lst.add_argument("project")
    lst.set_defaults(func=_cmd_list)

    info = sub.add_parser("info", help="Show one grant's full record.")
    info.add_argument("project")
    info.add_argument("grant_id")
    info.set_defaults(func=_cmd_info)

    rev = sub.add_parser("revoke", help="Revoke a grant by id.")
    rev.add_argument("project")
    rev.add_argument("grant_id")
    rev.set_defaults(func=_cmd_revoke)

    return p


def _reorder_argv(argv: list[str]) -> list[str]:
    """Rewrite the user's positional order into argparse's expected order.

    Documented use:
        court-grant <project> <subcmd> ...
        court-grant <project> <peer> <path>...     # implicit add

    argparse wants the subcommand first, so we shift things around.

    This function only touches *positional* arguments. Flags (anything
    starting with ``-``) are left in place, which means
    ``court-grant --debug <project> list`` still works in the future if
    we add global flags (today there are none, but we don't want the
    re-orderer to be the reason new flags break).
    """
    if not argv:
        return argv
    if argv[0] in ("-h", "--help"):
        return argv

    # Walk the token stream collecting positional indices (tokens that
    # don't start with '-'). The first positional is the project; the
    # second is either a subcommand or the implicit-add peer.
    positional_indices = [i for i, tok in enumerate(argv) if not tok.startswith("-")]
    if not positional_indices:
        return argv
    proj_idx = positional_indices[0]
    project = argv[proj_idx]
    pre_flags = argv[:proj_idx]  # global flags reserved for future use
    after = argv[proj_idx + 1:]

    if not positional_indices[1:]:
        # Only the project was given — no second positional. Leave as add
        # so argparse can complain about missing peer/paths.
        return pre_flags + ["add", project] + after

    second_token_idx_in_after = positional_indices[1] - proj_idx - 1
    second_token = after[second_token_idx_in_after]
    if second_token in SUBCMDS:
        # Subcommand form: drop the subcommand from ``after`` and put it up front.
        new_after = after[:second_token_idx_in_after] + after[second_token_idx_in_after + 1:]
        return pre_flags + [second_token, project] + new_after

    # Implicit ``add`` form.
    return pre_flags + ["add", project] + after


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
