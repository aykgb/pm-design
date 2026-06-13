# Worktree Session 管理架构

本文档描述 `scripts/session-worktree-mgr.py` 的 worktree / session 状态管理体系：架构全景、state 文件系统、核心生命周期、持久化模型、sidecar 集成，以及 overview / rewatch 一致性。

## 1. 架构全景

```text
                        PM session（main worktree）
                        ┌──────────────────────────────┐
                        │  ses_1469...（当前 PM）        │
                        │  ┌──────┬──────┬──────┬────┐  │
                        │  │ Clio │General│Janitor│Momus│── main agents
                        │  └──────┴──────┴──────┴────┘  │
                        └──────────────────────────────┘
                                    │ session dispatch
                                    ▼
           ┌──────────────────────────────────────────────────┐
           │              Pool（~/.worktrees/xidi-minimal）     │
           │                                                  │
           │  wt_1              wt_2              wt_N         │
           │  ┌──────────┐     ┌──────────┐     ┌──────────┐  │
           │  │ Daedalus │     │ Daedalus │     │ Daedalus │  │
           │  │ Themis   │     │ Themis   │     │ Themis   │  │
           │  │ QA       │     │ QA       │     │ QA       │  │
           │  │ Morpheus │     │ Morpheus │     │ Morpheus │  │
           │  └──────────┘     └──────────┘     └──────────┘  │
           │       ▲                                  │        │
           │       │      round-robin                 │        │
           │       └──────────────┼──────────────────┘        │
           │                  prepare                          │
           └──────────────────────────────────────────────────┘
                                   │ watch_session() × 6 call sites
                                   ▼
           ┌──────────────────────────────────────────────────┐
           │  Sidecar（session-status-server.mjs :4107）       │
           │  POST /watch ← 注册                              │
            │  GET /status → 状态查询（idle / busy / unwatch）  │
           │  DELETE /watch/:id ← 注销（无自动调用）           │
           └──────────────────────────────────────────────────┘
```

**三层结构**：
- **Main worktree**：PM session + 4 个 main agent，跨任务复用，`session dispatch` 派发
- **Pool worktrees**：每个 wt_N 有 4 个 agent（Daedalus / Themis / QA / Morpheus），round-robin 抢占，每任务重建 session
- **Sidecar**：独立进程追踪 session 状态，供 overview State 列查询

## 2. State 文件系统

### 2.1 物理布局

```
~/.worktrees/xidi-minimal/.state/
├── .pool_rr_index            ← round-robin 指针（wt_1 起）
├── wt_1.state               ← worktree 状态 + agent session ID
├── wt_2.state
├── ...
├── wt_N.state
└── sessions/
    └── <pm_session_id>/
        └── main.state        ← main agent session ID（按 PM session 隔离）
```

### 2.2 `wt_N.state` 字段

| 字段 | 写入时机 | 说明 |
| --- | --- | --- |
| `status` | prepare / release | `idle` 或 `busy` |
| `branch` | prepare / release | 当前 checkout 的分支名 |
| `wt_path` | prepare / release | worktree 物理路径 |
| `base_ref` | release | 基线 commit（通常 `origin/main`） |
| `initialized` | pool init | `"1"` 表示 worktree 已就绪 |
| `updated_at` | 每次 update_state | ISO 时间戳 |
| `Daedalus_session_id` | ensure_pool_sessions | 当前 session ID |
| `Themis_session_id` | ensure_pool_sessions | 当前 session ID |
| `QA_session_id` | ensure_pool_sessions | 当前 session ID |
| `Morpheus_session_id` | ensure_pool_sessions | 当前 session ID |
| `*_session_title` | ensure_pool_sessions | session 标题（如 `wt_1-Daedalus`） |

格式为 `key=value`，每行一条。空行和 `#` 开头行为注释。

### 2.3 `main.state` 字段

| 字段 | 写入时机 | 说明 |
| --- | --- | --- |
| `Clio_session_id` | sessions create | 当前 session ID |
| `General_session_id` | sessions create | 当前 session ID |
| `Janitor_session_id` | sessions create | 当前 session ID |
| `Momus_session_id` | sessions create | 当前 session ID |
| `*_session_title` | sessions create | session 标题 |

按 PM session 隔离：不同 PM 会话的 main agents 互不干扰。代码通过 `config.pm_session_id` 路由到 `sessions/<pm_sid>/main.state`。无 PM session 时回退到 `main.state`（全局）。

## 3. 核心生命周期

### 3.1 总览

```text
pool init ──→ [idle] ──→ prepare ──→ [busy] ──→ dispatch ──→ Themis/QA ──→ merge
                 ▲                                                              │
                 └────────────────── release ──────────────────────────────────┘
```

### 3.2 pool init：初始化 worktree

1. 循环 `i = 1..pool_size`：
   - `git worktree add --force wt_i <base_ref>`（或 `--force-copy` 从已有 worktree 复制）
   - `ensure_pool_sessions` → 为每个 agent 创建 OpenCode session → `persist_session` 写入 `wt_i.state`
   - 标记 `initialized=1`，`status=idle`
2. 全程持 `pool_lock`（mkdir 互斥锁）

### 3.3 prepare：抢占 worktree

1. **持 `pool_lock`**
2. **`find_idle_wt`**：从 `.pool_rr_index` 记录的起始位置开始，round-robin 扫描 status=`idle` + initialized 的 worktree，命中后更新指针到下一个
3. **checkout 任务分支**：`git checkout -b feat_PN_TM`（基于 `base_ref`）
4. **`ensure_pool_sessions`**：`recreate_always=True`，删除旧 session 并创建全新 session → 保证跨任务隔离，每次 prepare 都是冷启动
5. **`update_state`**：`status=busy`，`branch=<任务分支>`
6. 打印 dispatch 命令提示

**并发安全**：`pool_lock` 确保同一时刻只有一个 prepare/release/repair 操作。

### 3.4 dispatch：派发 agent

1. 查 sidecar `/status` 获取目标 session 当前状态
2. `--require-no-busy` 模式下，`busy`/`streaming` 状态拒绝派发
3. `POST /session/{sid}/prompt_async` 异步发送 prompt
4. 可选 `--notify-session`：spawn `idle-watch` 后台进程，监听 session busy→idle 后通知 PM

**dispatch 不修改 state 文件**——session 已在 prepare 时创建并持久化。

### 3.5 release：释放 worktree

1. **持 `pool_lock`**
2. 检查 worktree 是否 dirty：dirty 时拒绝（除非 `--force`，则 `git reset --hard && git clean -fd`）
3. `git reset --hard <base_ref>` + `git checkout <base_ref>` 回到基线
4. **`cleanup_stale_sessions`**：删除 >1 天的旧 session（OpenCode session + state 条目），为下次 prepare 腾空间
5. **`update_state`**：`status=idle`，`branch=""`

**关键**：release 只清理 state 指针和 stale session，不删除还在 1 天内的近期 session。这些 session 作为历史记录保留在 OpenCode 中，overview 可通过 recent filter 查看。

## 4. Session 持久化架构

### 4.1 两类持久化

| 类型 | 函数 | 写入目标 | 写入内容 |
| --- | --- | --- | --- |
| Worktree agent | `persist_session()` | `wt_N.state` | `agent_session_id` + `agent_session_title` |
| Main agent | `persist_main_session()` | `sessions/<pm_sid>/main.state` | 同上 |

**共同行为**：写入 state 后立即调用 `watch_session()` 注册到 sidecar。

### 4.2 ensure_pool_sessions 流程

`prepare` 和 `pool repair` 调用此函数保证 worktree 上有可用的 agent session：

```
ensure_pool_sessions(wt_id, agents, recreate_always=True):
  for agent in [Daedalus, Themis, QA, Morpheus]:
    ensure_session(agent):
      1. 查 state 文件中的 *_session_id
      2. 如果存在且未过期（≤1天），复用
      3. 如果过期或 recreate_always=True，删除旧 session → 创建新 session
    persist_session(wt_id, agent, session):
      → update_state(wt_id, {agent_session_id, agent_session_title})
      → watch_session(sid)
```

### 4.3 Stale session 清理

```
session 过期判定: time.updated > 1 day (STALE_SESSION_MS_DEFAULT)

release 时: cleanup_stale_sessions() → 删除过期 session 的 OpenCode 记录 + state 条目
prepare 时: recreate_always=True → 无条件重建（跨任务隔离）
```

## 5. Round-Robin 与并发控制

### 5.1 Round-robin

```
.pool_rr_index: 持久化的起始索引（默认 1 = wt_1）

find_idle_wt():
  start = read .pool_rr_index
  for offset in 0..pool_size-1:
    i = ((start - 1 + offset) % pool_size) + 1
    if wt_i.status == "idle" AND wt_i.initialized:
      write .pool_rr_index = (i == pool_size ? 1 : i + 1)
      return wt_i
  fail("no idle initialized worktree")
```

每次 prepare 从上次命中的下一个位置开始扫描，保证 worktree 均匀使用。

### 5.2 Pool lock

```python
@contextmanager
def pool_lock():
    lock_dir = pool_dir / ".grab.lock"
    lock_dir.mkdir()  # mkdir 在 POSIX 上是原子的
    try: yield
    finally: lock_dir.rmdir()
```

基于目录创建的互斥锁——同一池目录下同时只有一个操作持有锁。覆盖 `prepare`、`release`、`pool init`、`pool repair`。

## 6. Sidecar 集成架构

### 6.1 启动与恢复

```text
service start sidecar:
  1. 启动 session-status-server.mjs（Node 进程，监听 :4107）
  2. 等待 health check 通过
  3. rewatch_all_sessions() → 从 state 文件恢复 watch 列表

sidecar 启动时:
  - OPENCODE_SESSION_IDS env var（当前未设置）→ 空 watch 表
  - 仅靠 rewatch_all_sessions 恢复
```

### 6.2 运行时集成点

```text
sessions create ──→ persist_session ──→ watch_session()
prepare         ──→ ensure_pool_sessions ──→ persist_session ──→ watch_session()
watch start     ──→ watch_session()
overview        ──→ collect_overview ──→ watch_session() 所有展示 session
```

**所有路径最终都落到同一入口**：`POST sidecar/watch {"sessionID": "ses_..."}`。

### 6.3 状态消费

```text
overview State 列:
  GET sidecar/status → {sessionID: "idle"|"busy"|"unwatch", ...}
  不在 status 中的 session → 显示 "unwatch"

watch:
  轮询 sidecar/status → 检测 busy→idle 跳变 → 通知 PM
```

### 6.4 僵尸 watch 问题

目前无自动 unwatch 机制。session 一旦注册到 sidecar 就永久追踪，直到：
- 手动 `DELETE /watch/:id`
- Sidecar 进程重启（watch 表清空，靠 rewatch 重建）

这导致 sidecar 的 watched 集合随时间膨胀，远超 overview 实际显示的 session 数。

## 7. Overview / Rewatch 一致性

### 7.1 两套发现逻辑

| | overview | rewatch_all_sessions |
| --- | --- | --- |
| session 来源 | OpenCode 实时 API（`collect_wt_sessions`） | state 文件（`wt_*.state` + `main.state`） |
| PM session | ✅ 显示 | ❌ 不在 state 文件 |
| orphan agent | 可通过 `--show-orphan` 显示 | ❌ 不在 state 文件 |
| 过滤 | `_apply_recent_filter` + `_limit_per_agent` | 仅 `_apply_recent_filter` |
| 清理 | watch 所有展示项 | 只增不减 |

**二者来源不同，结果天然不一致**。要让 status 与 overview 对齐，需要统一发现逻辑（都用 OpenCode API）并增加 unwatch 清理。

### 7.2 当前差距

以实际运行数据为例（2026-06-12）：

| 指标 | 数量 |
| --- | --- |
| state 文件中的 session ID | 44 |
| rewatch 过滤后（3 天窗口） | 24 |
| overview 默认显示 | 36 |
| sidecar 实际 watch | 184 |

差距来自：① overview 发现 state 文件外的 session（PM、孤儿等）；② 6 个 `watch_session` 调用点长期累积，无清理。

## 8. 异常路径

| 场景 | 处理 |
| --- | --- |
| prepare 时无 idle worktree | `fail("no idle initialized worktree")` |
| release 时 worktree dirty | 拒绝（除非 `--force`：reset + clean） |
| pool dispatch 时 session busy | `--require-no-busy` 拒绝 |
| pool repair 时 worktree 损坏 | `--reset` / `--force-copy` 重建 |
| sidecar 重启 | `rewatch_all_sessions` 从 state 文件恢复 |
| state 文件丢失 | `read_state` 返回 `{}`，视为未初始化 |
| `.pool_rr_index` 丢失 | 重置为 1（wt_1 起扫） |
| pool lock 被占用 | `fail("another pool operation is running")` |

## 9. 文件索引

| 文件 | 角色 |
| --- | --- |
| `scripts/session-worktree-mgr.py` | 完整实现（L1~3042） |
| `scripts/session-status-server.mjs` | Sidecar 进程（Node，L1~648） |
| 命令操作参考 | 已内置 PM system prompt |
| `docs/development_workflow.md` | 7 步流水线流程 |
| `.pm/operational_conventions.md` §OC5 | 约束级规则 |
