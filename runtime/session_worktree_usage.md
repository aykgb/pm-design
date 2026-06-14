# Session Worktree Manager 使用准则

使用 `python3 scripts/session-worktree-mgr.py` 管理 OpenCode 多 agent、worktree pool、session 状态与任务派发。

## 命令模型

严格按资源选择命令，禁止臆造参数。

| 目标 | 使用 |
| --- | --- |
| 查看全局状态 | `overview` |
| 操作单个 session | `session ... ses_xxx` |
| 查询/批量管理 sessions | `sessions ... --wt/--main/...` |
| 管理 worktree pool | `pool ...` |
| 管理服务 | `service ...` |
| 管理 idle-watch | `watch ...` |

## 常用命令

### 全局查看

```bash
python3 scripts/session-worktree-mgr.py overview
python3 scripts/session-worktree-mgr.py overview --wt wt_1
python3 scripts/session-worktree-mgr.py overview --main
python3 scripts/session-worktree-mgr.py overview --all
```

### overview 显示选项

```bash
# 默认：隐藏 unwatch / unknown session（视觉简洁）
python3 scripts/session-worktree-mgr.py overview

# 显示 unwatch session（清理审计 / 找未派发过的 session；输入=0 的 session 可考虑 hard delete）
python3 scripts/session-worktree-mgr.py overview --show-unwatch

# JSON 输出（含完整 payload，用于脚本提取）
python3 scripts/session-worktree-mgr.py overview --format json
```

**占位行行为**：默认隐藏 unwatch session 后，若某个 wt 内的 session **全部**被隐藏，overview 会打印一行占位（commit `63d051c`）：

```
wt_5  <branch>  <commit>  clean  <Δ>  (无)  ...  (9 unwatch sessions hidden; pass --show-unwatch)
```

占位让 wt 不会因为所有 session 被隐藏而"消失"。常见成因：wt 从未 dispatch 过、pool release 后 stale、sidecar 重启后未重 watch。

### 单个 session

```bash
python3 scripts/session-worktree-mgr.py session show ses_xxx
python3 scripts/session-worktree-mgr.py session status ses_xxx
python3 scripts/session-worktree-mgr.py session last ses_xxx
python3 scripts/session-worktree-mgr.py session dispatch ses_xxx --task "..." --yes
```

> **`session show` Context 字段**：反映 session 最近一次已完成的 LLM 调用的上下文窗口大小。若 session 正在跑 tool call，可能显示 0——此时可等 busy→idle 后再查。

### 多个 sessions

```bash
python3 scripts/session-worktree-mgr.py sessions list --wt wt_1
python3 scripts/session-worktree-mgr.py sessions list --main
python3 scripts/session-worktree-mgr.py sessions list --wt wt_1 --agent Daedalus
python3 scripts/session-worktree-mgr.py sessions create --agent Janitor
```

### Worktree pool

```bash
python3 scripts/session-worktree-mgr.py pool status --verify
python3 scripts/session-worktree-mgr.py pool init --size 10
python3 scripts/session-worktree-mgr.py pool repair wt_1
python3 scripts/session-worktree-mgr.py pool prepare --branch feat_xxx [--agents AGENTS] [--force-branch]
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." [--yes] [--force]
python3 scripts/session-worktree-mgr.py pool continue wt_1 Daedalus [--yes]
python3 scripts/session-worktree-mgr.py pool release wt_1
```

**`pool prepare` 注意事项**：

- 只接受 `--branch` / `--agents` / `--force-branch`，**不接受 `--wt`**。
- 自动 round robin 按 wt_id 顺序选下一个 idle worktree，无须也无法手动指定。
- 返回分配的 wt_id（如 `wt_6`），后续 `dispatch` / `release` 使用该 wt_id。

### 服务

```bash
python3 scripts/session-worktree-mgr.py service status
python3 scripts/session-worktree-mgr.py service start all
python3 scripts/session-worktree-mgr.py service restart sidecar
```

### overview 渲染

- **终端输出**：busy / streaming 状态的 session 加粗显示，`[STUCK]` 后缀跟随加粗。
- **管道 / JSON 输出**：不加粗，纯文本。

## 标准派发流程

**1. 查看池状态：**

```bash
python3 scripts/session-worktree-mgr.py pool status --verify
```

**2. 准备任务 worktree：**

```bash
python3 scripts/session-worktree-mgr.py pool prepare --branch feat_xxx
```

自动 round robin 按 wt_id 顺序选下一个 idle worktree，返回分配的 wt_id。**不接受 `--wt` 手动指定**（commit `58fd78d` 之前的 PM agent 曾误用 `--wt`，已修复）。

**3. 正常任务直接派发：**

```bash
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." --yes
```

`dispatch --yes` 会自动启动 idle-watch；agent 从 busy 回到 idle 后会自动通知 PM session。

**3a. Stuck session 续接（pool continue）：**

```bash
# 保留已有分支 + commits → 新 session → 自动续接 prompt（含 git log 快照）
python3 scripts/session-worktree-mgr.py pool continue wt_1 Daedalus --yes
```

适用：session stuck 但 agent 已部分完成并推了 commits。与 `--force` 重写不同，`continue` 保留分支和已有工作，仅创建全新 session 续接。

```bash
# 预览续接 prompt（不实际派发）
python3 scripts/session-worktree-mgr.py pool continue wt_1 Daedalus --task "补充说明..." 
```

**3b. Stuck session 强制恢复（--force）：**

```bash
# 自动恢复（session busy > 15min 且 time.updated 无刷新 → 自动 hard-delete + 重建）
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." --yes

# 强制恢复（不限 staleness，直接 hard-delete + 重建，丢弃已有工作）
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." --force --yes
```

区别：

| 路径 | 场景 | 行为 | 分支 |
|------|------|------|------|
| 正常 dispatch | session idle | 复用旧 session | 不改变 |
| `continue` | stuck 但有 commits | 新 session + 续接 prompt | **保留**已有 branch |
| stale 自动恢复 | stuck > 15min | hard-delete + 重建 | 保留（同 wt，不 checkout 新 branch） |
| `--force` | 彻底卡死、无产出 | hard-delete + 重建 | 保留（同 wt） |

**4. 释放 worktree：**

```bash
python3 scripts/session-worktree-mgr.py pool release wt_1
```

## auto-compact（idle-watch 自动触发）

dispatch 后 idle-watch 检测到 busy→idle 时，若 session context 超过 300K，自动做 summarize 压缩，完成后通知 PM。

**触发条件**：仅 `busy -> idle` 边沿 + one-shot 模式（`continuous` / `initial-idle` / `idle-after-update` 不触发）。

**通知格式**：

| 场景 | PM 收到的 notify |
|------|-----------------|
| context ≤ 300K，不触发 | （无，silent exit） |
| compact 失败 | `[idle-notify:compact-failed] {error}` |
| compact 成功 | `[idle-notify:compact-done] {before}K→{after}K` |
| compact 期间 session 被复用 | `[idle-notify:compact-skipped] session reused` |

## 何时先预览派发

只有高风险任务才先预览，不加 `--yes`：

```bash
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..."
```

高风险任务包括：

- 会大范围删除、重命名、迁移文件。
- 会修改核心架构、状态机、权限、持久化格式。
- 会执行破坏性命令或影响未提交修改。
- 任务描述不完整，存在明显歧义。
- 用户明确要求先预览。

普通代码修改、review、补文档、局部修复，默认直接 `--yes`。

## 结果查看 fallback

正常依赖 dispatch 自动 idle-watch 通知，不主动查看 agent 最后回复。

只有以下情况才 fallback 查看：

- PM 没收到 idle-watch 通知。
- session 长时间 busy/streaming。
- 需要人工核对 agent 最终输出。
- idle-watch 进程异常退出。
- 用户明确要求查看结果。

Fallback 命令：

```bash
python3 scripts/session-worktree-mgr.py session status ses_xxx
python3 scripts/session-worktree-mgr.py session last ses_xxx
python3 scripts/session-worktree-mgr.py overview --wt wt_1
```

## 禁止误推断

### 错误命令形式

不要使用这些错误形式：

```bash
python3 scripts/session-worktree-mgr.py sessions list --session ses_xxx
python3 scripts/session-worktree-mgr.py sessions status --session ses_xxx
python3 scripts/session-worktree-mgr.py overview --session ses_xxx
python3 scripts/session-worktree-mgr.py status --session ses_xxx
python3 scripts/session-worktree-mgr.py last --session ses_xxx
```

正确替代：

```bash
python3 scripts/session-worktree-mgr.py session show ses_xxx
python3 scripts/session-worktree-mgr.py session status ses_xxx
python3 scripts/session-worktree-mgr.py session last ses_xxx
```

### 错误参数形式

不要给以下命令传不存在的参数：

```bash
python3 scripts/session-worktree-mgr.py pool prepare --branch feat_xxx --wt wt_1  # ❌ pool prepare 不接受 --wt
```

`pool prepare` 只接受 `--branch` / `--agents` / `--force-branch`。worktree 由 round robin 自动分配，无须手动指定。dispatch 时才传 wt_id 作为位置参数（`pool dispatch wt_1 ...`），不是 `--wt` 选项。

## 失败恢复

| 错误 | PM 行为 |
| --- | --- |
| OpenCode server not healthy | 不主动启动服务；提醒用户执行 `python3 scripts/session-worktree-mgr.py service start opencode` |
| sidecar not healthy | 不主动启动服务；提醒用户执行 `python3 scripts/session-worktree-mgr.py service start sidecar` |
| no idle initialized worktree | 可执行 `pool status --verify` 查看状态；需要初始化/修复时先向用户报告，再建议 `pool init` 或 `pool repair wt_N` |
| missing/stale session id | dispatch `--yes` 时自动 `ensure_session + persist_session`；preview 显示 `auto_create: true` + reason。仅在 OpenCode 端硬错误（服务不可达 / 配额满）时报错，需先修复 OpenCode 再重试 |
| worktree is dirty | 不主动丢弃修改；先报告 dirty 状态，用户确认后才可执行 `pool release wt_N --force` |
| stuck-notify（idle-watch 通知） | 消息含 target / wt / agent / stale 时长。有 commits → `pool continue`；无产出 → `--force` |
| session busy/streaming < 15min | 正常工作中，等待；确认为卡死时用 `continue` 或 `--force` |
| 未收到 idle-watch 通知 | 可 fallback 执行 `session status ses_xxx`，必要时 `session last ses_xxx` |

## 操作边界

- PM 可以主动执行只读检查命令，例如 `overview`、`pool status --verify`、`session status`、`session last`。
- PM 可以按任务流程执行 `pool prepare`、`pool dispatch ... --yes`、`pool continue`。
- Stuck session（busy > 15min 且 time.updated 无刷新）→ idle-watch 发送 stuck-notify（含 wt/agent 上下文）。PM 收到后判断：有 commits → `pool continue`；无产出 → `dispatch --force`。
- `pool continue` 保留已有 branch 和 commits，创建全新 session + 自动续接 prompt（含 `git log --oneline -10`）；无 `--yes` 时仅预览，不修改状态。
- PM 不主动启动、停止、重启服务。
- PM 不主动执行破坏性命令，例如 `pool release --force`、`session delete --hard`、批量 delete。`dispatch --force` / `pool continue` 的自动 hard-delete 除外。
- 遇到服务异常、dirty worktree、缺失 session、需要 repair/init 的情况，先向用户报告原因和建议命令。
- 只有用户明确确认后，才执行修复类或破坏性命令。

## 原则

- 单个 session 一律用 `session`。
- 多个 sessions 一律用 `sessions`。
- worktree 生命周期一律用 `pool`。
- 正常任务默认直接派发 `--yes`。
- 高风险任务才先预览派发。
- stuck session 有 commits → `pool continue`；无产出 → `dispatch --force`。
- dispatch 会自动 idle-watch；主动查看结果只作为 fallback。
- stuck-notify 消息含 wt / agent / stale 时长，无需手动查 overview。
- overview 全局一次 `/session?limit=2000` HTTP 调用，非 per-wt 多次调用。
- 不确定命令时先执行 `python3 scripts/session-worktree-mgr.py -h` 或对应子命令 `-h`。
- `pool prepare` 自动 round robin 分配 worktree，**不接受 `--wt`**。`pool dispatch` 的 wt_id 是位置参数（如 `wt_1`），不是 `--wt` 选项。
- 不要绕过 `pool prepare -> pool dispatch -> pool release` 生命周期。
- `pool continue` 在已有 wt 上直接派发新 session，不走 `prepare`（branch 已存在）。
- 修改前先确认 worktree、branch、session id 与 agent 对应关系。
