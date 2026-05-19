**中文** | [English](./lan-deployment.en.md)

# 局域网部署 —— 双机快速上手

把两个 `agent-court` project 通过同一个局域网连起来。无需公网 IP，无需 VPN ——
只要两台机器能在 LAN 上互通就能跑。

> 状态：PR-1（HTTP + 签名 + 角色白名单）、PR-2（策略引擎 + 路径/关键字门禁
> + pending-approval 桶）、PR-3（LLM 裁判 + 失败安全回落）、PR-4
> （sudo 风格临时授权，走 `court-grant`）都已上线。还没做：PR-5 多通道
> 人工审批（飞书 / 微信）、PR-6 IM 冗余、TLS。

## 先建立心智模型

一座"朝廷"住在**某台机器上的某个 project** ——
`$COURT_ROOT/projects/<project>/`。每个 project 有自己的密钥对、自己的
`peers.yaml`、自己的 `court_id`。同一台机器上的两个 project 互相**无法**
推断对方存在；对外它们就是两座独立的朝廷。

也就是说，**下面的每一步对每一对要联邦的 project 都得重复做一遍**。
给 `project-A` 生过一次密钥对，并**不**能让同机的 `project-B` 也能联邦。

## 准备工作

每台机器上：

1. `agent-court` 已经 checkout，`bin/` 在 PATH 上。
2. MCP server 的 venv 已经装好：
   ```bash
   cd /path/to/agent-court/mcp/court-mcp
   uv venv .venv
   uv pip install --python .venv/bin/python -e .
   ```
3. 你想联邦的 project 已经存在于 `$COURT_ROOT/projects/<project>/`。
   仓库里的示例 project 直接可用：
   ```bash
   cp -r projects/example ~/.agent-court/projects/example
   ```

## 1. 给 project 生成密钥对

在 **Alice** 的机器上，给 `example` 这个 project：

```
$ court-keygen example
[court-keygen] new keypair for project 'example':
  /Users/alice/.agent-court/projects/example/identity/priv.key  (mode 0600)
  /Users/alice/.agent-court/projects/example/identity/pub.key   (mode 0644)

public key      : MCowBQYDK2VwAyEAaG6...     # base64 ed25519 公钥
fingerprint     : 7a4c0b9e3d2f8a16          # SHA-256 前缀 16 hex 字符

Share both with the peer who will federate with this project.
They paste them into THEIR project's peers.yaml under the entry for you.
```

在 **Bob** 的机器上：同样地，给*他的* `example` project 跑一遍。
现在双方都各自在 project 的 `identity/` 下有了一份 per-project 的
`priv.key` / `pub.key`。

再跑一次 `court-keygen example` 是 no-op，除非加 `--force`。

## 2. 在 `court.yaml` 里启用联邦

默认 `court.yaml` 的 `federation:` 块是注释掉的 —— 守护进程拒绝启动。
取消注释（或自己写一份），配好白名单：

```yaml
# ~/.agent-court/projects/example/court.yaml
federation:
  enabled: true

  # 你在网络上的 court_id。默认 "<hostname>-<project>"。
  court_id: "alice-laptop-example"

  # 外部 peer 可以派给哪些 role。默认仅 foreman。
  expose_roles:
    - foreman

  # PR-2：策略引擎用这两个清单检查入站消息的 `attaches:` 字段。
  # allow 非空 + attach 未覆盖 → human_required。
  # deny 命中（这里 OR 硬编码层）→ denied。
  allow_paths:
    - "bus/foreman/inbox/**"
    - "shared/notes-public.md"
  deny_paths:
    - "prompts/**"
    - "shared/notes-private.md"
```

`expose_roles` 白名单和 `allow_paths` / `deny_paths` 都会被强制执行。
角色检查过了之后，策略引擎评级消息并路由到 inbox / pending-approval /
denied —— 详见下面 "策略门禁" 一节。

## 3. 交换指纹 + 公钥

线下（Signal、面对面 等等），每个 project 都交换：

| 字段 | 要发的值 |
|---|---|
| `court_id` | 对方会用什么名字引用你。默认 `<hostname>-<project>`；在 `court.yaml` 的 `federation.court_id` 里覆盖。 |
| `fingerprint` | `court-keygen` 输出的 16 字节 hex。让对方第一次粘公钥时能用眼睛核对一遍。 |
| `pub_key_b64` | 完整的 base64 公钥（`court-keygen` 输出里也有；或者 `cat $COURT_ROOT/projects/<project>/identity/pub.key`）。**运行时必需** —— 没有它对方无法校验你的签名。 |

## 4. 每边各写一份 `peers.yaml`

这个文件**住在 project 里面**，不是共享配置目录里。

Alice 的 `~/.agent-court/projects/example/peers.yaml`：

```yaml
self:
  court_id: "alice-laptop-example"
  pub_key_fingerprint: "7a4c0b9e3d2f8a16"     # 仅信息性

peers:
  - name: "Bob"
    court_id: "bob-laptop-example"
    url: "http://192.168.1.50:8765"
    pub_key_fingerprint: "f0e1d2c3b4a59687"
    pub_key_b64: "MCowBQYDK2VwAyEAhV0z..."     # Bob 的公钥
    relation: "sibling"                        # parent | child | sibling
```

Bob 的 `~/.agent-court/projects/example/peers.yaml` —— 对称，列出 Alice
（他这边也是 `relation: sibling`）。

IP 自己替换。Linux 用 `ip addr`，macOS 用 `ipconfig getifaddr en0` 看
本机 LAN 地址。

> `relation:` 字段取代了老版的 `role:`。loader 仍然向后兼容接受
> `role:`，但新写的配置用 `relation:`。在 PR-1 里它仅是信息性的；
> PR-2 会用它让策略规则按 relation 不同（比如 parent court 的派发
> 自动免审）。

## 5. 起接收端守护进程

每边、**每个要接收的 project** 都跑：

```bash
court-peer example
```

它会绑 `0.0.0.0:8765` 并暴露 `POST /inbox` + `GET /healthz`。
用 `--bind` 或 `COURT_PEER_BIND` 改绑地址：

```bash
COURT_PEER_BIND=192.168.1.50:9000 court-peer example
```

如果你在同一台机器上要联邦多个 project，给每个分一个端口：

```bash
COURT_PEER_BIND=0.0.0.0:8765 nohup court-peer example   > ~/.agent-court/logs/peer-example.log 2>&1 &
COURT_PEER_BIND=0.0.0.0:8766 nohup court-peer client-a  > ~/.agent-court/logs/peer-client-a.log 2>&1 &
COURT_PEER_BIND=0.0.0.0:8767 nohup court-peer ops       > ~/.agent-court/logs/peer-ops.log 2>&1 &
```

每个 project 一个不同的 peer URL（`http://host:8765` vs
`http://host:8766` 等）；远端在他们 `peers.yaml` 里**对该 project**
的条目就用对应的 URL。

身份、peers、policy、bus 目录都是 project 级的 —— 同一台机器上三个
守护进程实际上跑着三座独立的朝廷。被授权派活到 `example` 的远端
**绝不可能**够到 `client-a` 或 `ops`。

如果那个 project 的 `court.yaml` 关闭了联邦，守护进程会拒绝启动，
并指向配置块的位置提示你。

## 6. 从 Alice → Bob 发一条测试消息

从任何接到 Alice 的 `court-mcp` 的 MCP 客户端（Claude Code、Cursor、Zed、
自研助手）调：

```python
list_peers(project="example")
# 返回：{project, self: {court_id, fingerprint, federation_enabled, ...},
#         peers: [...]}.  Bob 起守护进程后 reachable=true。

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="hi from Alice — please look at issue #42",
    target_role="foreman",
)
# 返回：{
#   http_status: 200,
#   response: {
#     status: "accepted",
#     file_path: ".../bus/alice-laptop-example/inbox/<file>.md",
#     id: ...
#   }
# }
```

Bob 机器上文件会出现在：

```
~/.agent-court/projects/example/bus/alice-laptop-example/inbox/1715432400-7f3d2e1a-upstream-to-foreman.md
```

Bob 目前还得**手动**把文件投给他的 foreman —— `court-watcher` 守护进程
只监听本地 role 的 `*/outbox/`，peer-inbox 的文件就坐在那等人去读。
今天受支持的工作流：

```bash
# 在接收端，定期：
ls ~/.agent-court/projects/example/bus/*/inbox/*.md
# 把任何想投给 foreman 的文件提升：
mv .../bus/<peer-court-id>/inbox/<file>.md \
   .../bus/foreman/inbox/<file>.md
```

后续 PR 会教 `court-watcher` 在策略判定是 `auto_pass` 时也自动把
peer-inbox 路由到对应 role 的 inbox。

## 策略门禁（PR-2）

入站消息过了签名 + 角色检查之后，策略引擎给它评级，路由到磁盘上
三种位置之一：

| 判定 | 落到 | 含义 |
|---|---|---|
| `auto_pass` / `judge` | `bus/<peer>/inbox/` | 正常投递给 foreman |
| `human_required` | `bus/<peer>/pending-approval/` | 等审批 —— 得人手动 `mv` 到 inbox |
| `denied` | `bus/<peer>/denied/` | 仅审计；永远不到 foreman |

`dispatch_to_peer` 的响应里始终带判定结果，发送方 LLM 可以据此反应：

```json
{
  "http_status": 200,
  "response": {
    "status": "pending_approval",
    "decision": "human_required",
    "tier": "hard_rule",
    "reasons": ["sensitive keyword 'password' in body → human_required"],
    "file_path": ".../bus/alice-laptop-example/pending-approval/...md"
  }
}
```

### 可选：`policy.yaml`

加一份 `~/.agent-court/projects/example/policy.yaml` 来调默认 tier
和加自定义敏感词：

```yaml
default_tier: tier_b           # tier_a (人审) | tier_b (judge) | tier_c (自动)
sensitive_keywords:
  - "wire transfer"
  - "merger"
```

文件缺失时默认是 `tier_b` + 无额外关键词。

### 可选：LLM 裁判（PR-3）

消息落到 `tier_b → judge` 这一档时，守护进程会调一个 LLM CLI 在
`auto_pass` 和 `human_required` 之间裁决。没配的时候默认用 `claude`
（或者 `court.yaml` 里的 `default_cli`）和内建 prompt
`mcp/court-mcp/prompts/judge.md`。

```yaml
# ~/.agent-court/projects/example/court.yaml
default_cli: claude               # LLM 裁判也用这个

federation:
  enabled: true
  judge:
    # cli: claude                 # 单独给裁判覆盖 default_cli
    # model: haiku                # --model flag（透传给 CLI）
    # prompt_file: /etc/agent-court/strict-judge.md
    timeout_seconds: 30
    confidence_threshold: 0.6
```

裁判的 prompt 要求严格 JSON：

```
{"verdict": "auto_pass" | "human_required", "confidence": 0.0-1.0, "reason": "..."}
```

任何出错（CLI 找不到、超时、输出不可解析、置信度低于
`confidence_threshold`）都**失败安全**地变成 `human_required`。具体
失败原因保留在 `policy-log.jsonl` 的 `reasons` 数组里 —— 收到可疑投递
后 `tail` 一下。

用自定义 `prompt_file` 可以教裁判认识你 project 特定的风险面（比如
"任何提到计费接口的都强制 human_required"）。内建 prompt 故意写得很
通用。

### 临时授权（PR-4）

当**接收方**想短时间内放某个 peer 看一个不在 `allow_paths` 里的文件
（"就这 30 分钟看一下 `notes/q2-plan.md`"），或者想让一条消息免过软层
审查，他们就发一张授权而不是改 `court.yaml`。授权是**时效绑定 + peer
绑定**的，且只会**增加**能力 —— 硬编码 deny（`.ssh`、`.env`、`/etc`、
`credentials.json` 等等）和用户自己的 `deny_paths` 永远还是先赢。

两种授权：

| 类型 | 放宽什么 | 用在 |
|---|---|---|
| **path** | `allow_paths` | 想放过的 attach 不在静态白名单里 |
| **tier** | 那个 peer 的 `policy_tier`（一条 `--once` 或多条限时） | 想跳过一个已知放心批次的 judge / 人审 |

```bash
# Path 授权 —— 常用形式：`add` 是隐式的。
court-grant example bob "notes/**"
court-grant example bob "shared/draft-*.md" --ttl 2h

# Tier 授权 —— 加 --tier <tier>。带 --once 就是限一次。
court-grant example bob --tier tier_c --once          # 一次免审
court-grant example bob --tier tier_c --ttl 1h        # 限时信任窗口

# 列出某 project 全部（active + expired）的授权。
court-grant example list
# STATE     T ID         PEER  EXPIRES                       HITS DETAIL
# active    P 4616c19a   bob   2026-05-13T22:53:00+08:00     0    notes/**
# active    T 7fa20bd8   bob   2026-05-13T23:00:00+08:00     0    →tier_c [once]
# consumed  T 9a01ee3c   bob   2026-05-13T22:00:00+08:00     1    →tier_c [once]

# 查一条授权的详情。
court-grant example info 4616c19a
# id            : 4616c19a
# grant_type    : path
# state         : active
# granted_to    : bob
# paths         : ['notes/**']
# issued_ts     : 2026-05-13T22:23:00+08:00
# issued_by     : alice@laptop
# expires_ts    : 2026-05-13T22:53:00+08:00
# remaining     : 27m13s
# hit_count     : 2
# last_hit_ts   : 2026-05-13T22:35:18+08:00
# file          : /Users/alice/.agent-court/projects/example/grants/4616c19a.json

# 在 TTL 前杀掉一张授权。
court-grant example revoke 4616c19a
```

每张授权是 `$COURT_ROOT/projects/<p>/grants/<id>.json` 下的一个 JSON
文件，**原子写入**（`tempfile + os.replace`），所以守护进程在读目录时
**永远**看不到半写的记录。重启不会丢 —— 没有任何内存状态可丢。守护进程
每次入站请求时重读 `grants/`，所以一次新的 `mint` / `revoke` / 消费
都会在**下一条**消息上立刻生效，**无需重启**。

字段参考（磁盘上 JSON 的 shape）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 8 hex 字符；同时也是文件名。 |
| `grant_type` | `"path"` \| `"tier"` | 这张授权拧的是哪个旋钮。 |
| `granted_to` | string | Peer `court_id`。必须匹配入站消息的 `from_court`。 |
| `paths` | list[string] | （path 授权）OR 到 `allow_paths` 里的 glob。 |
| `target_tier` | string | （tier 授权）`tier_a` / `tier_b` / `tier_c`。 |
| `consume_on_use` | bool | （tier 授权）true 时第一次命中后标记 consumed。 |
| `consumed_ts` | string \| null | once 授权被消费的时间。未消费时为 null。 |
| `issued_ts` / `expires_ts` | string (ISO 8601) | TTL 边界。 |
| `issued_by` | string ≤ 128 | 自由格式的审计标签（`$USER@$HOST`）。 |
| `hit_count` | int | 这张授权命中过多少条入站消息。 |
| `last_hit_ts` | string \| null | 最近一次命中的时间戳。 |

同一组面孔通过 MCP 也暴露给被授予此权限的上游 LLM：

```python
grant_peer_access(
    project="example",
    peer_court_id="bob-laptop-example",
    paths=["notes/**"],
    ttl="1h",
)
# → {project, id, grant_type: "path", granted_to, paths, ...,
#    hit_count: 0, remaining_seconds: 3600}

grant_peer_tier(
    project="example",
    peer_court_id="bob-laptop-example",
    target_tier="tier_c",
    consume_on_use=True,
)
# → {project, id, grant_type: "tier", target_tier, consume_on_use, ...}

list_grants(project="example")
# → {project, active: [...], expired: [...]}（每条都含
#   grant_type、hit_count、remaining_seconds）

grant_info(project="example", grant_id="4616c19a")
# → {state: "active"|"expired", ...完整记录...}

revoke_grant(project="example", grant_id="4616c19a")
# → {ok: true, result: "revoked", grant_id}
# 错误：invalid_id | not_found | io_error
```

#### 安全特性（把 MCP 工具委派出去前最好知道这几条）

- **路径包容性**。每个授权入口都校验 `project` 是单一安全文件系统组件，
  并且解析后必须严格位于 `$COURT_ROOT/projects/` 之下。传
  `project="../foo"` 直接报错，**不会**变成任意文件系统访问。
- **TTL 上限**。`parse_ttl` 拒绝超过 1 年的值，所以 `datetime + ttl`
  算术不会溢出；MCP/CLI 把这种情况包成干净的 `invalid_argument`。
- **严格 JSON schema**。授权文件在读侧严格解析；缺字段、类型不对、
  过大（> 64 KB）都会被跳过并写一条警告到 `logs/peer-errors.log`，
  绝不会被静默接受。
- **原子写入**。Mint / record_hit / mark_consumed 都走同目录 tempfile
  + `os.replace`，所以读者迭代 `glob("*.json")` 时只可能看到旧内容
  或新内容，**永远**看不到撕裂的写入。
- **Peer 存在性检查（MCP）**。`grant_peer_access` /
  `grant_peer_tier` 在 `peers.yaml` 存在且 `peer_court_id` 不在里面
  时**拒绝**铸造（避免拼错名字造成的"孤儿"授权 —— 那种授权要是真有
  同名 peer 后来加入，会悄悄激活）。CLI 故意宽松一些，方便在
  `peers.yaml` 还没接好的引导阶段也能用。

当一条入站 `attaches:` 路径被一张活跃授权覆盖（而`allow_paths` 没有），
判定的 `reasons` 会显式标注，这是有用的审计信号：

```json
{
  "decision": "auto_pass",
  "reasons": ["attach 'notes/q2-plan.md' covered by active grant pattern 'notes/**'"]
}
```

授权可以扛守护进程重启。过期是在读时判定的（`is_active(now)`），所以
**系统时钟变动也不会**意外让已过期授权复活。

### 按 peer 设 tier（在 `peers.yaml` 里）

```yaml
peers:
  - name: "External vendor"
    court_id: "vendor-build-bot"
    relation: "sibling"
    policy_tier: "tier_a"          # 不信任：什么都进 pending-approval
```

### 试一试：`attaches` + `dispatch_to_peer`

```python
dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="please review the diff",
    attaches=["bus/foreman/inbox/diff.md"],   # 在 allow_paths 内
)
# → decision: judge（或 tier_c 时 auto_pass）

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="here is the prod password=hunter2",
)
# → decision: human_required（命中关键词）

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="have a look",
    attaches=["~/.ssh/id_ed25519"],
)
# → decision: denied（命中硬编码路径）
```

判定的轨迹追加到
`~/.agent-court/projects/example/logs/policy-log.jsonl`：

```bash
tail -f ~/.agent-court/projects/example/logs/policy-log.jsonl
```

### 批准一条 `pending-approval` 消息

目前还没有审批 UI（PR-5 会加终端 + 飞书 + 微信）。
现在只能眼睛过一遍然后手动 mv：

```bash
cd ~/.agent-court/projects/example/bus/alice-laptop-example
cat pending-approval/*.md           # 看正文 + policy_reasons
mv pending-approval/<file>.md inbox/   # 放出去给 foreman
```

## 防火墙清单

`court-peer` 是纯 HTTP，没有 TLS。每台机器双向放行那个端口 —— 大多数
家庭 LAN 本来就敞着。

| 操作系统 | 放行入站 TCP 8765 |
|---|---|
| macOS | `系统设置 → 网络 → 防火墙 → 选项 → 给 court-peer 的 python 二进制设 "允许"` |
| Ubuntu | `sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp` |
| Windows | `New-NetFirewallRule -DisplayName "agent-court" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow` |

## 故障排查

### `dispatch_to_peer` 响应里出现 `transport_error`

- 验证 URL 通：在 Alice 上 `curl http://192.168.1.50:8765/healthz`。
- `curl` 卡住 → 防火墙在丢包。看上面的防火墙清单。
- `Connection refused` → 那个 IP/端口上的守护进程没起。
  `ps aux | grep peer_daemon` 查一下。

### 401 `bad_signature` 或 `missing_peer_pub_key`

- 对端拒了你的签名。基本是这几种：
  - 对端 project 的 `peers.yaml` 里你 `court_id` 对应的 `pub_key_b64`
    和你当前的 `priv.key` 不匹配。重新跑过 `court-keygen`？得把新公钥
    重新分给对方。
  - 你和对端对"签名 payload 包含哪些字段"看法不一致。两边必须是同一个
    `agent-court` 版本。
  - 你 `dispatch_to_peer` 的 `project=...` 指错了，用来签名的私钥
    属于另一个 court，不是对方期待的那个。
- 看接收端的 `$COURT_ROOT/projects/<project>/logs/peer-errors.log`
  看具体失败原因。

### 403 `federation_disabled`

- 对端 `court.yaml` 没有 `federation:` 块，或者 `federation.enabled:
  false`。这个 flag 是按请求重读的，所以对端把它翻回 `true` **无需**
  重启守护进程。

### 403 `unknown_sender`

- 你的 `court_id` 不在对端 `peers.yaml` 里。让对方加上，或者检查你
  用的 `court_id` 和他们配的一致。记住：**每个 project 有自己的
  `peers.yaml`** —— 被列在他们 `project-A/peers.yaml` 里并不能让你
  访问 `project-B`。

### 403 `role_not_exposed`

- 你派给了一个不在对端 `federation.expose_roles` 里的 role。默认只
  暴露 `foreman`；要么走 foreman，要么让对方把目标 role 加进
  `expose_roles`。

### 响应里 `decision: denied`

- 某个 attach 命中了 deny 规则（你的或硬编码的）。消息**没**投递 ——
  它坐在接收端的 `bus/<your-court-id>/denied/` 里仅供审计。看响应的
  `reasons` 字段：
  ```
  "reasons": ["attach '/etc/passwd' hits hardcoded deny '/etc/**'"]
  ```
- 硬编码 deny **既不能**通过 `court.yaml` 解锁，**也不能**通过 PR-4
  授权解锁 —— 设计如此。如果你确实需要那条路径，重构这次派发（比如
  把相关内容粘进消息正文）。如果只是被你自己的
  `allow_paths`/`deny_paths` 挡了，让对方铸造临时授权：
  `court-grant <project> <your-court-id> "<glob>"`（见上面的"临时授权"）。

### `decision: human_required` / `status: pending_approval`

- 要么发送方的 peer 条目是 `policy_tier: tier_a`，要么正文触发了
  敏感词，要么 attach 落在 `allow_paths` 外，要么 PR-3 LLM 裁判把
  一条本来能过的 tier_b 升级到了 human_required。文件在接收端的
  `bus/<your-court-id>/pending-approval/` 里；得人手动 `mv` 到
  `inbox/` 才算投递。
- 看接收端 `logs/policy-log.jsonl` —— 每条判定都有 `reasons` 数组
  解释哪条规则触发了。

### 日志里看到 `tier: llm_judge_failed`

- PR-3 裁判尝试调 LLM CLI 失败了。看消息的 `reasons` 数组 —— 它给出
  具体失败：
  - `"cli '<x>' not found on PATH"` → 接收端机器装上那个 CLI，或者
    把 `federation.judge.cli` 指向一个存在的。
  - `"<x> timed out after Ns"` → CLI 跑太慢。要么调高
    `federation.judge.timeout_seconds`，要么换用 `judge.model`
    指更快的模型。
  - `"<x> exited <n>: ..."` → CLI 出错（往往是配额或鉴权）。手动
    跑同样命令复现。
  - `"no JSON object found in LLM output"` / `"verdict must be ..."`
    → 模型偏离了 JSON 契约。把 prompt 收紧，或者换模型。
- 所有这些情况都坍塌成 `human_required` —— 所以裁判配错**永远不会**
  误投递消息；只会**过度**标记。你可以慢慢修。

### `list_peers` 显示 `reachable: false`

- 对端守护进程没起或不可达。和上面 transport_error 一样的检查。

## LAN 之外

跨网段：

- **推荐**：两台机器都装 [tailscale](https://tailscale.com)，在
  `peers.yaml` 里用 tailscale 分配的 IP。从这往后和 LAN 一样，还附带
  端到端加密。
- **自托管**：用 `frp` 或 `cloudflared` 把 court-peer 端口暴露出去。
  公网 URL 写进 `peers.yaml`。如果流量真的穿越公网，配上真正的 TLS
  反向代理（PR-1 不发 TLS）。

两种方式都会在后续 PR 里通过 `docs/networking.md` 文档化。
