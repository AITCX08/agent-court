# PR-12 手动验收清单

1. `python3 -m gitea_client whoami` 能返回当前账号
2. `python3 -m gitea_client list_assigned_issues` 能列出指派 issue
3. `bin/gitea-watcher --once` 能写出 `seen-issues.json`
4. pending issue 能生成 `pending-shenli/*.md`
5. `python3 -m shenli --input <pending.md>` 输出合法 JSON
6. GO issue 会创建 `~/.agent-court/projects/issue-<repo_slug>-<num>/`
7. `tmux ls` 出现 `agent-court-issue-<repo_slug>-<num>` session
8. foreman 回执会被回写到 issue 评论
9. NEED_INFO 不会 spawn 新 court
10. REJECT 会评论并关闭 issue
