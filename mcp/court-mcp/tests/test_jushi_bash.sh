#!/usr/bin/env bash
# End-to-end smoke for jushi daemon (PR-8).
#
# Verifies:
#   1. daemon --once consumes a fake claude jsonl and writes a session .md
#   2. redaction placeholder shows up for a planted password line
#   3. cursor is created so a re-run is idempotent (kept=0)
#   4. court-record decision/note land in the expected dirs
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
VENV_PY="$REPO/mcp/court-mcp/.venv/bin/python"
[ -x "$VENV_PY" ] || { echo "[FAIL] venv missing at $VENV_PY"; exit 1; }

TMP_CR="$(mktemp -d -t jushi-cr-XXXX)"
TMP_CLAUDE="$(mktemp -d -t jushi-claude-XXXX)"
trap "rm -rf $TMP_CR $TMP_CLAUDE" EXIT

fail() { echo "[FAIL] $*" >&2; exit 1; }
pass() { echo "[PASS] $*"; }

# ---------------------------------------------------------------------------
# 1. Set up a fake demo project with a jushi block
# ---------------------------------------------------------------------------
mkdir -p "$TMP_CR/projects/demo/logs"
cat > "$TMP_CR/projects/demo/court.yaml" <<EOF
project: demo
session: demo
attach_window: foreman
roles: []
jushi:
  enabled: true
  cwd_prefixes:
    - /sandbox
EOF

# Fake jsonl with one clean turn + one secret turn + one out-of-scope turn.
SESSION_ID="abc-1234"
JSONL_DIR="$TMP_CLAUDE/-sandbox-demo"
mkdir -p "$JSONL_DIR"
JSONL="$JSONL_DIR/$SESSION_ID.jsonl"
cat > "$JSONL" <<EOF
{"type":"permission-mode","permissionMode":"yes","sessionId":"$SESSION_ID"}
{"type":"user","message":{"role":"user","content":"clean question about caching"},"uuid":"u-clean","timestamp":"2026-05-19T10:00:00.000Z","cwd":"/sandbox/demo","sessionId":"$SESSION_ID"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"the password is hunter2"}]},"uuid":"u-secret","timestamp":"2026-05-19T10:00:05.000Z","cwd":"/sandbox/demo","sessionId":"$SESSION_ID"}
{"type":"user","message":{"role":"user","content":"out of scope"},"uuid":"u-out","timestamp":"2026-05-19T10:00:10.000Z","cwd":"/other","sessionId":"$SESSION_ID"}
EOF

# ---------------------------------------------------------------------------
# 2. Run daemon --once
# ---------------------------------------------------------------------------
COURT_ROOT="$TMP_CR" CLAUDE_PROJECTS_DIR="$TMP_CLAUDE" "$VENV_PY" \
  "$REPO/mcp/court-mcp/jushi_daemon.py" --project demo --once --no-summarize \
  >/dev/null 2>&1 || fail "daemon --once exited non-zero"
pass "daemon --once completed"

# ---------------------------------------------------------------------------
# 3. Session file landed in 2026-05-19
# ---------------------------------------------------------------------------
SESSION_FILE="$TMP_CR/projects/demo/memory/dynamic/sessions/2026-05-19/$SESSION_ID.md"
[ -f "$SESSION_FILE" ] || fail "session file not at $SESSION_FILE"
pass "session file generated"

# Clean turn preserved
grep -q "clean question about caching" "$SESSION_FILE" \
  || fail "clean turn missing in session file"
pass "clean turn preserved"

# Secret-bearing turn redacted: raw text is gone, placeholder is present
if grep -q "hunter2" "$SESSION_FILE"; then
  fail "secret leaked into session file"
fi
grep -q "redacted: keyword=password" "$SESSION_FILE" \
  || fail "redaction placeholder missing"
pass "secret redacted with placeholder"

# Out-of-cwd row filtered out
if grep -q "out of scope" "$SESSION_FILE"; then
  fail "cwd_prefixes filter ignored"
fi
pass "cwd_prefixes filter dropped /other row"

# ---------------------------------------------------------------------------
# 4. Cursor exists + idempotency on re-run
# ---------------------------------------------------------------------------
CURSOR="$TMP_CR/projects/demo/memory/dynamic/.cursors/$SESSION_ID.json"
[ -f "$CURSOR" ] || fail "cursor file missing at $CURSOR"
pass "cursor file written"

LOG="$TMP_CR/projects/demo/logs/jushi.log"
> "$LOG"
COURT_ROOT="$TMP_CR" CLAUDE_PROJECTS_DIR="$TMP_CLAUDE" "$VENV_PY" \
  "$REPO/mcp/court-mcp/jushi_daemon.py" --project demo --once --no-summarize \
  >/dev/null 2>&1 || fail "second daemon --once non-zero"
# Second run should be silent: no "run_once: kept=" line at all because
# kept/redacted/removed are all 0 (logger gated on non-zero).
if grep -q "run_once: kept=[1-9]" "$LOG"; then
  fail "second --once was not idempotent: $(grep run_once $LOG)"
fi
pass "second --once idempotent"

# ---------------------------------------------------------------------------
# 5. court-record decision + note
# ---------------------------------------------------------------------------
COURT_ROOT="$TMP_CR" "$REPO/bin/court-record" decision "test decision" \
  --project demo --body "picked X over Y" --tag smoke >/dev/null \
  || fail "court-record decision failed"
COURT_ROOT="$TMP_CR" "$REPO/bin/court-record" note "test note" \
  --project demo --body "watch out for Z" >/dev/null \
  || fail "court-record note failed"

DECISION_COUNT="$(find "$TMP_CR/projects/demo/memory/dynamic/decisions" -name '*.md' | wc -l | tr -d ' ')"
NOTE_COUNT="$(find "$TMP_CR/projects/demo/memory/dynamic/notes" -name '*.md' | wc -l | tr -d ' ')"
[ "$DECISION_COUNT" = "1" ] || fail "expected 1 decision, got $DECISION_COUNT"
[ "$NOTE_COUNT" = "1" ] || fail "expected 1 note, got $NOTE_COUNT"
pass "court-record wrote 1 decision + 1 note"

echo
echo "All jushi smoke checks passed."
