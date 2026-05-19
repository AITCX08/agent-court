#!/usr/bin/env bash
set -euo pipefail

echo "[e2e] optional script; requires real git.k2lab.ai access and a disposable test repo" >&2
echo "[e2e] create issue -> assign to self -> run bin/gitea-watcher --once -> verify seen file/session/comments" >&2
