#!/usr/bin/env bash
# Smoke test for bin/court-onboard.
#
# Verifies the bash-side behavior that is hard to cover in pytest:
#   1. --verify on an empty court-root prints a checklist and exits 0
#   2. install_dotfiles backs up an existing ~/.tmux.conf and installs ours
#   3. install_dotfiles is idempotent (re-running does not re-create a backup
#      if the file already matches)
#
# Real brew/uv install steps are not exercised here; tests/test_onboard.py
# covers the Python side. We source court-onboard with ONBOARD_NO_MAIN=1 so
# the bash functions are callable without running main().

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SCRIPT="$REPO/bin/court-onboard"

TMP_HOME="$(mktemp -d -t onboard-home-XXXX)"
TMP_COURT_ROOT="$(mktemp -d -t onboard-cr-XXXX)"
cleanup() { rm -rf "$TMP_HOME" "$TMP_COURT_ROOT"; }
trap cleanup EXIT

fail() { echo "[FAIL] $*" >&2; exit 1; }
pass() { echo "[PASS] $*"; }

# ---------------------------------------------------------------------------
# 1. --verify on empty court-root
# ---------------------------------------------------------------------------
out="$("$SCRIPT" --verify --court-root "$TMP_COURT_ROOT" 2>&1)"
echo "$out" | grep -q "Welcome to agent-court" \
  || fail "verify checklist missing 'Welcome to agent-court'"
echo "$out" | grep -q "court-up demo" \
  || fail "verify checklist missing next-steps hint"
pass "verify on empty court-root prints checklist"

# ---------------------------------------------------------------------------
# 2. install_dotfiles backs up + installs
# ---------------------------------------------------------------------------
HOME="$TMP_HOME"
export HOME
mkdir -p "$HOME"
echo "user-existing-tmux-config" > "$HOME/.tmux.conf"

# shellcheck disable=SC1090
ONBOARD_NO_MAIN=1 source "$SCRIPT"

REPO_ROOT="$REPO"
install_dotfiles >/dev/null

[ -f "$HOME/.tmux.conf" ] || fail "new tmux.conf missing after install_dotfiles"
new_first_line="$(head -1 "$HOME/.tmux.conf")"
[ "$new_first_line" = "set -g mouse on" ] \
  || fail "tmux.conf first line should be 'set -g mouse on', got: $new_first_line"

backup_count="$(ls "$HOME"/.tmux.conf.bak.* 2>/dev/null | wc -l | tr -d ' ')"
[ "$backup_count" -ge 1 ] || fail "expected at least one tmux.conf backup, found $backup_count"

backup_content="$(cat "$HOME"/.tmux.conf.bak.*)"
[ "$backup_content" = "user-existing-tmux-config" ] \
  || fail "backup did not preserve original content (got: $backup_content)"
pass "install_dotfiles backs up existing tmux.conf"

# fish conf.d snippet has REPO_ROOT placeholder rewritten
snippet="$HOME/.config/fish/conf.d/agent-court.fish"
[ -f "$snippet" ] || fail "fish conf.d snippet missing at $snippet"
grep -qF "$REPO_ROOT/bin" "$snippet" \
  || fail "snippet missing absolute REPO_ROOT/bin"
grep -q '<<<REPO_ROOT>>>' "$snippet" \
  && fail "placeholder still present in $snippet"
pass "fish snippet PATH placeholder rewritten"

# ---------------------------------------------------------------------------
# 3. Idempotent re-run produces no spurious backup
# ---------------------------------------------------------------------------
# Take a snapshot of existing backups, then re-install. cmp -s in
# install_dotfiles should detect identical content and skip the backup.
before_backups="$(ls "$HOME"/.tmux.conf.bak.* 2>/dev/null | wc -l | tr -d ' ')"
sleep 1   # ensure any new backup would get a distinct timestamp
install_dotfiles >/dev/null
after_backups="$(ls "$HOME"/.tmux.conf.bak.* 2>/dev/null | wc -l | tr -d ' ')"
[ "$before_backups" = "$after_backups" ] \
  || fail "re-running install_dotfiles produced extra backups ($before_backups -> $after_backups)"
pass "install_dotfiles idempotent on identical files"

echo
echo "All smoke checks passed."
