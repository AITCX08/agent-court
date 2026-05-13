"""agent-court — shenpi terminal channel.

Pushes a notification to the project's ``shared/event.log`` so that
anyone with ``tail -f`` open sees the pending-approval landing in real
time. Also writes a banner to stderr — useful when the daemon is being
run interactively (``tongzheng <project>`` in a terminal) — but only
when stderr is a TTY, so we don't clutter background logs.

No outbound network. Always succeeds (or silently swallows OSError to
let the other channels still fire).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


async def send(item, shenpi_cfg) -> None:  # noqa: ARG001 — cfg unused here
    """Append a one-line notification to event.log + (optionally) stderr."""
    line = _format_line(item)

    # event.log lives at <project>/shared/event.log; create dir if needed.
    # filepath is <project>/bus/<peer>/pending-approval/<file>.md, so we
    # walk up four levels to reach <project>.
    project_dir = item.filepath.parent.parent.parent.parent
    log = project_dir / "shared" / "event.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("a") as f:
            f.write(line + "\n")
    except OSError:
        # Don't crash the daemon over a log-append; raise so shenpi.notify
        # records ``notify_failed`` and tries the next channel.
        raise

    if sys.stderr.isatty():
        try:
            sys.stderr.write(_format_banner(item, line))
            sys.stderr.flush()
        except OSError:
            pass


def _format_line(item) -> str:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return (
        f"{ts} | shenpi/留中 | from={item.peer} (msg_from={item.msg_from}) "
        f"to={item.msg_to} id={item.msg_id} | "
        f"reasons={_truncate(item.reasons, 120)} | "
        f"body={_truncate([item.body or ''], 80)}"
    )


def _format_banner(item, line: str) -> str:
    rule = "─" * 70
    return (
        f"\n{rule}\n"
        f"  [shenpi/留中] {item.project}: 一条来自 '{item.peer}' 的消息需要批准\n"
        f"  id={item.msg_id}\n"
        f"  reasons: {', '.join(item.reasons[:3]) or '(无明文原因)'}\n"
        f"  approve : court-approve {item.project} approve {item.msg_id}\n"
        f"  deny    : court-approve {item.project} deny {item.msg_id}\n"
        f"{rule}\n"
    )


def _truncate(seq, n: int) -> str:
    if not seq:
        return ""
    s = " | ".join(str(x) for x in seq)
    return s if len(s) <= n else s[: n - 1] + "…"


# Register at module import time so shenpi.notify can dispatch to us.
from shenpi import _register_channel  # noqa: E402

_register_channel("terminal", send)
