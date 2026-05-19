#!/usr/bin/env bash
# dashboard 模式终端审批 e2e 最小闭环
#
# 验证:
# 1. watcher 抓到新 issue 后 (用 mock fixture, 不连真 Gitea) 写 pending-intake-context + queue intake
# 2. 模拟 IM/终端 reply approve 写 .result
# 3. ImReplyRouter.scan_once() 读到 .result → 调 spawn-issue-window
# 4. seen-issues.json 出现 DISPATCHED_DASHBOARD 或 SPAWN_FAILED (取决于 tmux session 是否真起)
#
# 不依赖: 真 Gitea / 真 Claude CLI / 真 tmux session.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY_DIR="$ROOT/mcp/court-mcp"
PYTHON_BIN="$PY_DIR/.venv/bin/python3"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "[e2e] 找不到 venv python: $PYTHON_BIN; 跳过" >&2
  exit 0
fi

# 临时 COURT_ROOT, 完全本地隔离
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agent-court-dashboard-e2e.XXXXXX")"
trap "rm -rf '$TMP_ROOT'" EXIT

mkdir -p "$TMP_ROOT/gitea-watcher/pending-approval" \
         "$TMP_ROOT/gitea-watcher/pending-intake-context" \
         "$TMP_ROOT/gitea-watcher/pending-shenli"

REPO="K2Lab/test-e2e"
NUM=999
SLUG="k2lab-test-e2e"

# 1. 预先写一个 intake-context fixture
cat > "$TMP_ROOT/gitea-watcher/pending-intake-context/${SLUG}-${NUM}.json" <<JSON
{
  "issue": {
    "number": ${NUM},
    "title": "e2e fixture",
    "html_url": "http://localhost/${REPO}/issues/${NUM}",
    "body": "fixture body",
    "labels": [],
    "repository": {"full_name": "${REPO}"}
  },
  "decision": {
    "decision": "GO",
    "court_project_name": "issue-${SLUG}-${NUM}",
    "branch_prefix": "auto/issue-${NUM}/",
    "agent_team_plan": {}
  },
  "comments": []
}
JSON

# 2. 模拟 IM/终端 reply approve, 写 .result (绕过 fcntl 流程, 直接写文件)
SLUG_ID="${SLUG}-${NUM}-intake"
cat > "$TMP_ROOT/gitea-watcher/pending-approval/${SLUG_ID}.result" <<JSON
{
  "repo": "${REPO}",
  "number": ${NUM},
  "stage": "INTAKE",
  "verdict": "approve",
  "winner": "terminal",
  "reason": "",
  "edit_instruction": "",
  "at": "2026-05-19T20:30:00Z"
}
JSON

# 3. 跑一次 router.scan_once(). 我们手动用 stub 替换 spawn-issue-window 避免真起 tmux
# (设定 PATH 把 stub 放前面, stub 仅 exit 0)
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/spawn-stub.XXXXXX")"
trap "rm -rf '$TMP_ROOT' '$STUB_DIR'" EXIT
cat > "$STUB_DIR/spawn-issue-window" <<'STUB_EOF'
#!/usr/bin/env bash
echo "[stub] spawn-issue-window called: $*" >&2
exit 0
STUB_EOF
chmod +x "$STUB_DIR/spawn-issue-window"

cd "$PY_DIR"
COURT_ROOT="$TMP_ROOT" \
PATH="$STUB_DIR:$PATH" \
"$PYTHON_BIN" - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
# 用环境里指定的 stub 路径
court_root = Path(os.environ["COURT_ROOT"])
# 找 stub 位置 (PATH 第一个)
stub_bin = None
for p in os.environ["PATH"].split(":"):
    candidate = Path(p) / "spawn-issue-window"
    if candidate.exists() and ".stub" in str(candidate.parent) or "spawn-stub" in str(candidate):
        stub_bin = candidate
        break

# fallback: 用 default bin
if stub_bin is None:
    for p in os.environ["PATH"].split(":"):
        candidate = Path(p) / "spawn-issue-window"
        if candidate.exists():
            stub_bin = candidate
            break

# stub GiteaClient (router reject 路径才用, e2e 跑 approve 不会调到, 但 import 时 spec 安全)
class _StubGitea:
    def comment_on_issue(self, *args, **kwargs): pass
    def transition_issue(self, *args, **kwargs): pass

from im_reply_router import ImReplyRouter
router = ImReplyRouter(court_root, gitea_client=_StubGitea(), spawn_window_bin=stub_bin)
n = router.scan_once()
print(f"router scanned: {n}")

# 4. 验证 seen-issues.json
import json
seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
key = f"K2Lab/test-e2e#999"
entry = seen.get(key, {})
print(f"seen entry: {entry}")
assert entry.get("last_action") == "DISPATCHED_DASHBOARD", f"expected DISPATCHED_DASHBOARD, got {entry.get('last_action')!r}"
assert entry.get("approval_winner") == "terminal", f"winner mismatch: {entry.get('approval_winner')!r}"

# 5. 验证 .result 被 archive
processed = list((court_root / "gitea-watcher" / "pending-approval" / ".processed").glob("*"))
assert len(processed) == 1, f"expected 1 archived result, got {len(processed)}: {[p.name for p in processed]}"

print("[e2e] dashboard intake approve flow OK")
PY

echo "[e2e] dashboard 模式终端审批 minimal e2e PASS"
