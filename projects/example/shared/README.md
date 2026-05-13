# Example project — `example`

This is the reference project shipped with `agent-yamen`. It's a generic
full-stack web team:

| Role | Responsibility |
|---|---|
| **zongguan** | Dispatches incoming work to the right worker. |
| **frontend** | UI / client code. |
| **backend** | Server / API / database. |
| **devops** | Infra / deploy / CI. |

Replace the placeholder `work_dir` paths in `yamen.yaml` with the actual
paths to your repo, then start it:

```bash
kaifu example
```

Bus state lives in `bus/<role>/{inbox,outbox,inbox/.done}` and is
gitignored — it's runtime data, not project source.
