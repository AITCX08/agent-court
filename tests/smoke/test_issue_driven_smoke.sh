#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="$ROOT/mcp/court-mcp/.venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi
cd "$ROOT/mcp/court-mcp"

"$PYTHON_BIN" -m gitea_client --help >/dev/null
"$ROOT/bin/gitea-watcher" help >/dev/null
"$PYTHON_BIN" -m shenli --help >/dev/null
"$PYTHON_BIN" -c "import gitea_watcher" >/dev/null
test -f "$ROOT/.claude/skills/shenli/SKILL.md"
echo "smoke ok"
