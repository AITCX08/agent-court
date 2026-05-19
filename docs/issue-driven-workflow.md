# 工单驱动模式

## 1. 粒度迁移说明

旧路径是人工 `court-send`；新路径是 Gitea issue 指派给你后，由 `gitea-watcher`
自动拉取并交给 `shenli` 决策。

## 2. 启停命令

```bash
bin/gitea-watcher install
bin/gitea-watcher start
bin/gitea-watcher status
bin/gitea-watcher --once
bin/gitea-watcher logs
bin/gitea-watcher stop
```

## 3. token 来源链

1. `git credential-osxkeychain get`
2. `K2LAB_GIT_TOKEN`
3. `~/.netrc`

绝不把 token 写到 `.env` 或 `.git-credentials`。

## 4. 审理决策表

- `GO`: 描述完整、范围明确、包含验收标准
- `NEED_INFO`: 缺少仓库信息、复现步骤或验收标准
- `REJECT`: 命中 `wontfix` / `duplicate` / `out-of-scope`

## 5. 故障排查

- `401`: 用 `security` 或重新登录 Gitea 刷新 keychain 条目，不要把 token 写进 plist
- `pending-shenli` 卡住：手工运行 `python3 -m shenli --input <file>` 看 JSON/报错
- `court-up` 起不来：检查 `yq`、`tmux`、目标仓库目录和 `court.yaml`
- 连续 5 次 5xx：watcher 会通知并退出，稍后重启

## 6. 安全注意

- token 不落盘
- 自动派活只允许 issue 对应仓库
- 自动生成的 dispatch 强制限制 branch prefix、禁 force push、禁改 main、commit trailer 必填
