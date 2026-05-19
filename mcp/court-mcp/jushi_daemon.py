"""agent-court PR-8 jushi daemon -- dynamic-memory collector.

Watches ~/.claude/projects/*/*.jsonl for new claude-code conversation turns,
extracts user + assistant messages, runs them through the local redaction
pipeline (jushi_redact), and persists the survivors into a court project's
dynamic-memory tree:

    $COURT_ROOT/projects/<project>/memory/dynamic/
      ├── sessions/<YYYY-MM-DD>/<session_id>.md   # per-session daily log
      ├── decisions/<YYYY-MM-DD>-<slug>.md        # user-tagged via record skill
      ├── notes/<YYYY-MM-DD>-<slug>.md            # user-tagged via record skill
      ├── .cursors/<session_id>.json              # byte-offset cursor
      └── .summary/<session_id>.txt               # cached one-liner summaries

Why not run as a global daemon? Because court is multi-project and each
project may want different redaction rules + cwd filters. The daemon is
project-scoped; run multiple instances if you have multiple projects.

CLI:
    jushi_daemon.py --project demo                # one-shot + watch loop
    jushi_daemon.py --project demo --once         # one pass, exit
    jushi_daemon.py --project demo --interval 30  # poll every 30s (default)
    jushi_daemon.py --project demo --no-summarize # skip LLM summarization
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from bangjiao import assert_safe_path_component, court_root


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def claude_projects_dir() -> Path:
    """Where claude code drops one jsonl per session.

    Override via $CLAUDE_PROJECTS_DIR for tests."""
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def project_memory_dir(project: str) -> Path:
    return court_root() / "projects" / project / "memory" / "dynamic"


def project_log_path(project: str) -> Path:
    return court_root() / "projects" / project / "logs" / "jushi.log"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class JushiConfig:
    project: str
    cwd_prefixes: list[str] = field(default_factory=list)
    redact_extra_keywords: list[str] = field(default_factory=list)
    redact_extra_patterns: list[str] = field(default_factory=list)
    summarize: bool = True
    summary_interval_turns: int = 5
    housekeeping_days: int = 30
    max_lines_per_session: int = 10000
    autostart: bool = False


def load_jushi_config(project: str) -> JushiConfig:
    """Parse the `jushi:` block out of court.yaml. Missing block = defaults."""
    assert_safe_path_component(project, field_name="project")
    court_yaml = court_root() / "projects" / project / "court.yaml"
    if not court_yaml.exists():
        raise FileNotFoundError(
            f"court.yaml not found at {court_yaml} "
            f"-- run court-onboard --court-root {court_root()} first"
        )

    data = yaml.safe_load(court_yaml.read_text()) or {}
    block = data.get("jushi") or {}

    return JushiConfig(
        project=project,
        cwd_prefixes=[os.path.expandvars(os.path.expanduser(p))
                      for p in (block.get("cwd_prefixes") or [])],
        redact_extra_keywords=list(block.get("redact_extra_keywords") or []),
        redact_extra_patterns=list(block.get("redact_extra_patterns") or []),
        summarize=bool(block.get("summarize", True)),
        summary_interval_turns=int(block.get("summary_interval_turns", 5)),
        housekeeping_days=int(block.get("housekeeping_days", 30)),
        max_lines_per_session=int(block.get("max_lines_per_session", 10000)),
        autostart=bool(block.get("autostart", False)),
    )


# ---------------------------------------------------------------------------
# Directory bootstrap
# ---------------------------------------------------------------------------

def ensure_memory_dirs(project: str) -> Path:
    base = project_memory_dir(project)
    for sub in ("sessions", "decisions", "notes", ".cursors", ".summary"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Main loop (filled in by T8.2 + T8.4)
# ---------------------------------------------------------------------------

class GracefulExit(Exception):
    """Raised on SIGTERM/SIGINT to flush state before dying."""


def _install_signal_handlers() -> None:
    def _handler(signum, frame):
        raise GracefulExit(f"received signal {signum}")
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_once(cfg: JushiConfig, log: logging.Logger) -> int:
    """One pass over claude jsonls. Returns number of new turns persisted."""
    from jushi_extract import scan_jsonl_dir
    from jushi_redact import RedactionRules, apply_rules
    from jushi_writer import append_turn, housekeeping

    rules = RedactionRules().extend(
        extra_keywords=cfg.redact_extra_keywords,
        extra_patterns=cfg.redact_extra_patterns,
    )
    memory = project_memory_dir(cfg.project)

    kept = redacted = 0
    for turn in scan_jsonl_dir(
        claude_projects_dir(), memory, cwd_prefixes=cfg.cwd_prefixes or None,
    ):
        result = apply_rules(turn.text, rules)
        append_turn(memory, turn, result,
                    project=cfg.project, max_lines=cfg.max_lines_per_session)
        if result.kept:
            kept += 1
        else:
            redacted += 1

    removed = housekeeping(memory, retain_days=cfg.housekeeping_days)
    if kept or redacted or removed:
        log.info("run_once: kept=%d redacted=%d removed_days=%d",
                 kept, redacted, removed)
    return kept + redacted


def run_forever(cfg: JushiConfig, interval: int, log: logging.Logger) -> int:
    log.info(
        "jushi watching project=%s interval=%ds cwd_prefixes=%s",
        cfg.project, interval, cfg.cwd_prefixes or "<all>",
    )
    try:
        while True:
            try:
                run_once(cfg, log)
            except Exception:
                log.exception("run_once failed; will retry")
            time.sleep(interval)
    except GracefulExit as e:
        log.info("graceful exit: %s", e)
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging(project: str, verbose: bool) -> logging.Logger:
    log_path = project_log_path(project)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("jushi")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    # Only mirror to stderr when stderr is a real tty; under nohup the
    # parent shell pipes stderr into the same log file and we get
    # duplicate lines.
    if sys.stderr.isatty():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jushi_daemon.py",
        description="agent-court dynamic-memory collector (PR-8 jushi)",
    )
    p.add_argument("--project", required=True,
                   help="court project name; reads its court.yaml jushi: block")
    p.add_argument("--once", action="store_true",
                   help="run a single pass and exit (no watch loop)")
    p.add_argument("--interval", type=int, default=30,
                   help="seconds between scans in watch loop (default: 30)")
    p.add_argument("--no-summarize", action="store_true",
                   help="skip LLM-based one-liner summary generation")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_jushi_config(args.project)
    if args.no_summarize:
        cfg.summarize = False

    ensure_memory_dirs(cfg.project)
    log = _setup_logging(cfg.project, args.verbose)
    _install_signal_handlers()

    log.info("jushi starting: project=%s claude_dir=%s memory=%s",
             cfg.project, claude_projects_dir(), project_memory_dir(cfg.project))

    if args.once:
        run_once(cfg, log)
        return 0
    return run_forever(cfg, args.interval, log)


if __name__ == "__main__":
    raise SystemExit(main())
