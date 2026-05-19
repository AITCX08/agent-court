#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

WORK_REPO="$TMP/work"
REMOTE_REPO="$TMP/remote.git"
PLAN_JSON="$TMP/plan.json"
COURT_ROOT="$TMP/court-root"

mkdir -p "$COURT_ROOT/projects"
git init "$WORK_REPO" >/dev/null
git init --bare "$REMOTE_REPO" >/dev/null
git -C "$WORK_REPO" remote add origin "$REMOTE_REPO"
git -C "$WORK_REPO" config user.name tester
git -C "$WORK_REPO" config user.email tester@example.com

cat >"$PLAN_JSON" <<EOF
{
  "roles": [
    {"name": "foreman", "work_dir": "$WORK_REPO", "cli": "claude", "model": "sonnet-4.6"}
  ],
  "session": "agent-court-issue-k2lab-demo-7",
  "branch_prefix": "auto/issue-7/",
  "issue_ref": "K2Lab/demo#7"
}
EOF

COURT_ROOT="$COURT_ROOT" "$ROOT/bin/migrate-to-court" --new issue-k2lab-demo-7 --plan "$PLAN_JSON" >/dev/null

echo ok >"$WORK_REPO/file.txt"
git -C "$WORK_REPO" add file.txt
git -C "$WORK_REPO" commit -m "seed" -m "Issue: K2Lab/demo#7" >/dev/null

git -C "$WORK_REPO" branch -M main
if git -C "$WORK_REPO" push origin main >/dev/null 2>&1; then
  echo "expected main push to fail" >&2
  exit 1
fi

git -C "$WORK_REPO" checkout -b wrong-prefix >/dev/null
if git -C "$WORK_REPO" push origin wrong-prefix >/dev/null 2>&1; then
  echo "expected wrong prefix push to fail" >&2
  exit 1
fi

git -C "$WORK_REPO" checkout -b auto/issue-7/good >/dev/null
echo bad >>"$WORK_REPO/file.txt"
git -C "$WORK_REPO" add file.txt
git -C "$WORK_REPO" commit -m "missing trailer" >/dev/null
if git -C "$WORK_REPO" push origin auto/issue-7/good >/dev/null 2>&1; then
  echo "expected missing trailer push to fail" >&2
  exit 1
fi

git -C "$WORK_REPO" reset --hard HEAD~1 >/dev/null
echo good >>"$WORK_REPO/file.txt"
git -C "$WORK_REPO" add file.txt
git -C "$WORK_REPO" commit -m "with trailer" -m "Issue: K2Lab/demo#7" >/dev/null
git -C "$WORK_REPO" push origin auto/issue-7/good >/dev/null

echo "pre-push hook ok"
