"""User-driven memory tagging for PR-8 jushi.

Two kinds:

* ``decision`` -- a high-signal "I picked X over Y because Z" entry.
  These are what the PR-10 review agent will preferentially cite.
* ``note``     -- a free-form working note: gotcha, observation, etc.

Storage:

    $MEMORY/dynamic/decisions/<YYYY-MM-DD>-<slug>.md
    $MEMORY/dynamic/notes/<YYYY-MM-DD>-<slug>.md

Each file has a small frontmatter and the body is raw markdown. We do
NOT redact decisions or notes -- they are user-authored, the user knows
what they wrote.

CLI:
    record.py decision "haystack scoring" --body "switched to BM25 because..."
    record.py note     "ETL gotcha"        --body "products_raw has dup pk..."
    record.py list     --kind decision     # list recent entries
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bangjiao import assert_safe_path_component, court_root


KINDS = ("decision", "note")


# ---------------------------------------------------------------------------
# Slug
# ---------------------------------------------------------------------------

def slugify(title: str, max_len: int = 32) -> str:
    """Kebab-case ASCII slug. Non-ASCII collapses to a short hash so Chinese
    titles still produce a stable filename rather than dropping to empty.
    """
    base = title.strip().lower()
    base = re.sub(r"[^\w\s-]", "", base, flags=re.UNICODE)
    base = re.sub(r"\s+", "-", base)
    base = base[:max_len].strip("-")
    if not base or not any(c.isascii() for c in base):
        import hashlib
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        base = f"untitled-{digest}"
    return base


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def memory_kind_dir(project: str, kind: str) -> Path:
    assert kind in KINDS, f"kind must be one of {KINDS}"
    return court_root() / "projects" / project / "memory" / "dynamic" / f"{kind}s"


def record_path(project: str, kind: str, title: str, *,
                date: Optional[datetime] = None) -> Path:
    d = date or datetime.now(timezone.utc)
    return memory_kind_dir(project, kind) / f"{d.strftime('%Y-%m-%d')}-{slugify(title)}.md"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_record(
    project: str,
    kind: str,
    title: str,
    body: str,
    *,
    tags: Optional[list[str]] = None,
) -> Path:
    assert_safe_path_component(project, field_name="project")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not title.strip():
        raise ValueError("title must be non-empty")

    now = datetime.now(timezone.utc)
    path = record_path(project, kind, title, date=now)
    path.parent.mkdir(parents=True, exist_ok=True)

    fm_lines = [
        "---",
        f"kind: {kind}",
        f"title: {title}",
        f"created_at: {now.isoformat()}",
        f"project: {project}",
    ]
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    fm_lines.append("---")
    fm_lines.append("")

    body_text = body.rstrip() + "\n"
    path.write_text("\n".join(fm_lines) + "\n" + body_text, encoding="utf-8")
    return path


def list_records(project: str, kind: Optional[str] = None,
                 limit: int = 20) -> list[Path]:
    kinds = [kind] if kind else list(KINDS)
    out: list[Path] = []
    for k in kinds:
        d = memory_kind_dir(project, k)
        if d.exists():
            out.extend(sorted(d.glob("*.md"), reverse=True))
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_body(args: argparse.Namespace) -> str:
    if args.body:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("body required (--body, --body-file, or stdin)")


def _cmd_write(args: argparse.Namespace) -> int:
    body = _read_body(args)
    path = write_record(args.project, args.kind, args.title, body, tags=args.tag)
    print(path)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    for path in list_records(args.project, kind=args.kind, limit=args.limit):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="record.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    for kind in KINDS:
        sp = sub.add_parser(kind, help=f"record a {kind}")
        sp.add_argument("title")
        sp.add_argument("--project", required=True)
        sp.add_argument("--body", default=None)
        sp.add_argument("--body-file", default=None)
        sp.add_argument("--tag", action="append", default=None)
        sp.set_defaults(func=_cmd_write, kind=kind)

    lp = sub.add_parser("list", help="list recent records")
    lp.add_argument("--project", required=True)
    lp.add_argument("--kind", choices=list(KINDS), default=None)
    lp.add_argument("--limit", type=int, default=20)
    lp.set_defaults(func=_cmd_list)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
