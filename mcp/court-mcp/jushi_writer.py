"""Append turns to per-day session files. PR-8 jushi.

Layout:

    $MEMORY/dynamic/sessions/<YYYY-MM-DD>/<session_id>.md

Each file has a small one-time frontmatter (written when the file is
first created) and append-only body. We never rewrite frontmatter on
re-runs -- summary lookups walk forward through the file, so stale
``last_seen`` would be wasted work.

A turn that fails redaction is still recorded (in ``placeholder`` mode)
so the log preserves time alignment; only the offending text is gone.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from jushi_extract import Turn
from jushi_redact import RedactionResult


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def session_file_for(memory_dir: Path, turn: Turn) -> Path:
    """Resolve the per-day file path for one turn.

    Falls back to today's date if the turn lacks a timestamp.
    """
    day = _day_part(turn.timestamp) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return memory_dir / "sessions" / day / f"{turn.session_id}.md"


def _day_part(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    # ISO format is YYYY-MM-DDTHH:MM:SS... Slice is safe even for shorter
    # variants because the leading 10 chars are always the date.
    return iso_ts[:10] if len(iso_ts) >= 10 else ""


def _time_part(iso_ts: str) -> str:
    return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def _frontmatter(turn: Turn, *, project: str) -> str:
    lines = [
        "---",
        f"session_id: {turn.session_id}",
        f"first_turn_at: {turn.timestamp or 'unknown'}",
        f"project: {project}",
        f"cwd: {turn.cwd or '<unset>'}",
    ]
    if turn.git_branch:
        lines.append(f"git_branch: {turn.git_branch}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


def _body_block(turn: Turn, result: RedactionResult) -> str:
    """One markdown section per turn.

    Placeholder mode lets a redaction sit in the timeline without leaking;
    drop mode skips the section entirely (caller filters before us).
    """
    if not result.kept and result.placeholder is None:
        return ""

    text = turn.text if result.kept else (result.placeholder or "")
    ts = _time_part(turn.timestamp) or "??:??:??"

    return f"\n## {turn.role} · {ts} · {turn.uuid[:8]}\n\n{text}\n"


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------

def append_turn(
    memory_dir: Path,
    turn: Turn,
    result: RedactionResult,
    *,
    project: str,
    max_lines: Optional[int] = None,
) -> Path:
    """Persist one turn. Returns the file written to.

    If ``max_lines`` is set and the file would cross the threshold, we
    rotate to ``<session_id>.<N>.md`` where N is the smallest unused
    suffix. This keeps single-file size manageable for long-running
    sessions.
    """
    if not result.kept and result.placeholder is None:
        # Drop mode + redaction hit -> persist nothing.
        return Path()

    file_path = session_file_for(memory_dir, turn)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    body = _body_block(turn, result)

    if not file_path.exists():
        head = _frontmatter(turn, project=project)
        file_path.write_text(head + body, encoding="utf-8")
        return file_path

    if max_lines is not None:
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                line_count = sum(1 for _ in f)
        except OSError:
            line_count = 0
        if line_count > max_lines:
            rotated = _next_rotation(file_path)
            shutil.move(str(file_path), str(rotated))
            head = _frontmatter(turn, project=project)
            file_path.write_text(head + body, encoding="utf-8")
            return file_path

    with file_path.open("a", encoding="utf-8") as f:
        f.write(body)
    return file_path


def _next_rotation(base: Path) -> Path:
    """Pick ``<stem>.<N>.md`` for the smallest unused N."""
    stem = base.stem
    n = 1
    while True:
        candidate = base.with_name(f"{stem}.{n}{base.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def housekeeping(memory_dir: Path, *, retain_days: int) -> int:
    """Remove ``sessions/<date>/`` dirs older than ``retain_days``.

    Returns the count of removed directories. Errs on the side of caution:
    only deletes directories whose name parses as YYYY-MM-DD.
    """
    sessions = memory_dir / "sessions"
    if not sessions.exists():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).date()
    removed = 0
    for entry in sessions.iterdir():
        if not entry.is_dir():
            continue
        try:
            day = datetime.strptime(entry.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(entry)
            removed += 1
    return removed
