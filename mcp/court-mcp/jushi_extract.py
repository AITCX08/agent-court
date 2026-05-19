"""Incremental jsonl scanner for PR-8 jushi.

Pure functions: no I/O outside the file system, no global state. The
daemon (jushi_daemon.py) is the only caller that schedules them.

Claude code persists one jsonl per session under
``~/.claude/projects/<encoded-cwd>/<session_uuid>.jsonl``. Each line is a
JSON object; ``type`` distinguishes the record kind. We care about
``user`` and ``assistant`` -- everything else (file snapshots, permission
mode toggles, tool calls) is metadata for the cli and not for the
human-readable memory.

A cursor file at ``$MEMORY/.cursors/<session_id>.json`` records the byte
offset where we stopped, so re-runs only read the tail. The cursor is
robust to truncation: if the jsonl has fewer bytes than the cursor we
reset to 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


# ---------------------------------------------------------------------------
# Turn dataclass
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """One user or assistant message ready for redaction + persistence."""
    session_id: str
    role: str            # "user" or "assistant"
    timestamp: str       # ISO 8601 UTC
    cwd: str             # cwd at the time of the turn (may be "")
    git_branch: str      # gitBranch field (may be "")
    text: str            # flattened content (assistant blocks joined)
    uuid: str            # unique id from the jsonl row


# ---------------------------------------------------------------------------
# Cursor I/O
# ---------------------------------------------------------------------------

def cursor_path(memory_dir: Path, session_id: str) -> Path:
    return memory_dir / ".cursors" / f"{session_id}.json"


def read_cursor(memory_dir: Path, session_id: str) -> int:
    path = cursor_path(memory_dir, session_id)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
        return int(data.get("offset", 0))
    except (json.JSONDecodeError, ValueError):
        # Cursor corrupted -- safer to re-scan from 0 than to silently skip.
        return 0


def write_cursor(memory_dir: Path, session_id: str, offset: int) -> None:
    path = cursor_path(memory_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "offset": offset,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))


# ---------------------------------------------------------------------------
# Content flattening
# ---------------------------------------------------------------------------

def _flatten_content(content) -> str:
    """user.content is a string; assistant.content is a list of blocks.

    We keep only ``text`` blocks. Tool calls and their results are skipped
    by design -- they bloat the log and rarely add semantic value over the
    surrounding human/assistant prose.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if txt:
                    chunks.append(txt)
        return "\n".join(chunks)
    return ""


# ---------------------------------------------------------------------------
# Row -> Turn
# ---------------------------------------------------------------------------

def parse_row(row: dict, *, session_id: str) -> Optional[Turn]:
    """Convert one jsonl row into a Turn, or None to skip.

    Skip rules:
      - type not in {user, assistant}
      - message.content is empty after flattening
      - command-name / command-message wrappers (slash commands logged as
        meta-user rows) -- they duplicate the actual user turn that follows
    """
    rtype = row.get("type")
    if rtype not in ("user", "assistant"):
        return None

    message = row.get("message") or {}
    role = message.get("role") or rtype
    if role not in ("user", "assistant"):
        return None

    content = message.get("content")
    text = _flatten_content(content).strip()
    if not text:
        return None

    # Skip slash-command meta rows (claude inserts a synthetic <command-name>
    # tag inside content when the user types /foo -- this is the literal
    # command echo, not the user's intent prose).
    if text.startswith("<command-name>") or text.startswith("<command-message>"):
        return None

    return Turn(
        session_id=session_id,
        role=role,
        timestamp=row.get("timestamp", ""),
        cwd=row.get("cwd", "") or "",
        git_branch=row.get("gitBranch", "") or "",
        text=text,
        uuid=row.get("uuid", ""),
    )


# ---------------------------------------------------------------------------
# Incremental iteration
# ---------------------------------------------------------------------------

def session_id_from_jsonl(path: Path) -> str:
    """File name is ``<uuid>.jsonl``; strip extension."""
    return path.stem


def iter_new_turns(
    jsonl_path: Path,
    memory_dir: Path,
    *,
    cwd_prefixes: Optional[list[str]] = None,
) -> Iterator[Turn]:
    """Yield Turns from ``jsonl_path`` that we have not seen before.

    Advances the cursor in lockstep so a crash mid-scan replays the
    not-yet-yielded tail next run.
    """
    session_id = session_id_from_jsonl(jsonl_path)
    if not jsonl_path.exists():
        return

    size = jsonl_path.stat().st_size
    start = read_cursor(memory_dir, session_id)
    if start > size:
        # File truncated or rotated; restart.
        start = 0

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(start)
        while True:
            line = f.readline()
            if not line:
                break
            offset_after = f.tell()

            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Partial line (mid-write); stop without advancing past it.
                break

            # cwd filter (if cwd_prefixes is set, only keep matching rows).
            if cwd_prefixes:
                row_cwd = row.get("cwd", "") or ""
                if not any(row_cwd.startswith(p) for p in cwd_prefixes):
                    write_cursor(memory_dir, session_id, offset_after)
                    continue

            turn = parse_row(row, session_id=session_id)
            write_cursor(memory_dir, session_id, offset_after)
            if turn is not None:
                yield turn


def scan_jsonl_dir(
    claude_dir: Path,
    memory_dir: Path,
    *,
    cwd_prefixes: Optional[list[str]] = None,
) -> Iterator[Turn]:
    """Walk every ``*.jsonl`` under ``claude_dir`` and yield new turns.

    Order: by jsonl mtime so the most recently active sessions surface first.
    """
    if not claude_dir.exists():
        return

    jsonls: list[Path] = []
    for entry in claude_dir.iterdir():
        if entry.is_dir():
            jsonls.extend(entry.glob("*.jsonl"))
        elif entry.suffix == ".jsonl":
            jsonls.append(entry)

    jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in jsonls:
        yield from iter_new_turns(path, memory_dir, cwd_prefixes=cwd_prefixes)
