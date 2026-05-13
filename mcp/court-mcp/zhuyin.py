"""agent-court — generate a project's identity keypair.

Invoked by ``bin/court-keygen <project>``. Writes to
``$COURT_ROOT/projects/<project>/identity/``.

Identity is per-project on purpose: each project has its own pubkey so
peers of project A can't even know project B exists on the same machine.
"""

from __future__ import annotations

import argparse
import sys

from bangjiao import (
    generate_keypair,
    load_identity,
    project_dir,
    project_priv_key_path,
    project_pub_key_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate this project's ed25519 keypair under "
                    "$COURT_ROOT/projects/<project>/identity/.",
    )
    parser.add_argument("project", help="project name under $COURT_ROOT/projects/")
    parser.add_argument("--force", action="store_true", help="overwrite existing keypair")
    args = parser.parse_args()

    project = args.project
    if not project_dir(project).is_dir():
        print(
            f"[zhuyin] project '{project}' not found at {project_dir(project)}",
            file=sys.stderr,
        )
        print(
            "[zhuyin] create the project directory first "
            "(copy projects/example or scaffold by hand).",
            file=sys.stderr,
        )
        return 1

    if project_priv_key_path(project).exists() and not args.force:
        identity = load_identity(project)
        print(
            f"[zhuyin] keypair already exists at {project_priv_key_path(project)}",
            file=sys.stderr,
        )
        print(
            "[zhuyin] use --force to regenerate (this invalidates trust with all peers)",
            file=sys.stderr,
        )
        print()
        print(f"project         : {project}")
        print(f"public key      : {identity.pub_b64}")
        print(f"fingerprint     : {identity.fingerprint}")
        print()
        print("Share the fingerprint AND the public key with peers; they paste")
        print(f"both into their peers.yaml under the entry for project '{project}'.")
        return 0

    identity = generate_keypair(project, force=args.force)
    print(f"[zhuyin] new keypair written for project '{project}':")
    print(f"  {project_priv_key_path(project)}  (mode 0600)")
    print(f"  {project_pub_key_path(project)}   (mode 0644)")
    print()
    print(f"project         : {project}")
    print(f"public key      : {identity.pub_b64}")
    print(f"fingerprint     : {identity.fingerprint}")
    print()
    print("Share the fingerprint AND the public key with peers. Each peer pastes")
    print("them into the entry for this court_id in their peers.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
