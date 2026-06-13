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

### 单个 session

```bash
python3 scripts/session-worktree-mgr.py session show ses_xxx
python3 scripts/session-worktree-mgr.py session status ses_xxx
python3 scripts/session-worktree-mgr.py session last ses_xxx
python3 scripts/session-worktree-mgr.py session dispatch ses_xxx --task "..." --yes
```

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
python3 scripts/session-worktree-mgr.py pool prepare --branch feat_xxx
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." --yes
python3 scripts/session-worktree-mgr.py pool release wt_1
```

### 服务

```bash
python3 scripts/session-worktree-mgr.py service status
python3 scripts/session-worktree-mgr.py service start all
python3 scripts/session-worktree-mgr.py service restart sidecar
```

## 标准派发流程

**1. 查看池状态：**

```bash
python3 scripts/session-worktree-mgr.py pool status --verify
```

**2. 准备任务 worktree：**

```bash
python3 scripts/session-worktree-mgr.py pool prepare --branch feat_xxx
```

**3. 正常任务直接派发：**

```bash
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..." --yes
```

`dispatch --yes` 会自动启动 idle-watch；agent 从 busy 回到 idle 后会自动通知 PM session。正常情况下不用主动轮询 agent 结果。

**4. 释放 worktree：**

```bash
python3 scripts/session-worktree-mgr.py pool release wt_1
```

## 何时先预览派发

只有高风险任务才先预览，不加 `--yes`：

```bash
python3 scripts/session-worktree-mgr.py pool dispatch wt_1 Daedalus --task "..."
```

高风险任务包括：

* 会大范围删除、重命名、迁移文件。
* 会修改核心架构、状态机、权限、持久化格式。
* 会执行破坏性命令或影响未提交修改。
* 任务描述不完整，存在明显歧义。
* 用户明确要求先预览。

普通代码修改、review、补文档、局部修复，默认直接 `--yes`。

## 结果查看 fallback

正常依赖 dispatch 自动 idle-watch 通知，不主动查看 agent 最后回复。

只有以下情况才 fallback 查看：

* PM 没收到 idle-watch 通知。
* session 长时间 busy/streaming。
* 需要人工核对 agent 最终输出。
* idle-watch 进程异常退出。
* 用户明确要求查看结果。

Fallback 命令：

```bash
python3 scripts/session-worktree-mgr.py session status ses_xxx
python3 scripts/session-worktree-mgr.py session last ses_xxx
python3 scripts/session-worktree-mgr.py overview --wt wt_1
```

## 禁止误推断

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

## 失败恢复

| 错误 | PM 行为 |
| --- | --- |
| OpenCode server not healthy | 不主动启动服务；提醒用户执行 `python3 scripts/session-worktree-mgr.py service start opencode` |
| sidecar not healthy | 不主动启动服务；提醒用户执行 `python3 scripts/session-worktree-mgr.py service start sidecar` |
| no idle initialized worktree | 可执行 `pool status --verify` 查看状态；需要初始化/修复时先向用户报告，再建议 `pool init` 或 `pool repair wt_N` |
| missing/stale session id | 可建议 `pool repair wt_N`；是否执行由用户确认 |
| worktree is dirty | 不主动丢弃修改；先报告 dirty 状态，用户确认后才可执行 `pool release wt_N --force` |
| session busy/streaming | 可执行 `session status ses_xxx` 查看状态；等待、换 session 或重新派发前需判断风险 |
| 未收到 idle-watch 通知 | 可 fallback 执行 `session status ses_xxx`，必要时 `session last ses_xxx` |

## 操作边界

* PM 可以主动执行只读检查命令，例如 `overview`、`pool status --verify`、`session status`、`session last`。
* PM 可以按任务流程执行 `pool prepare`、`pool dispatch ... --yes`。
* PM 不主动启动、停止、重启服务。
* PM 不主动执行破坏性命令，例如 `pool release --force`、`session delete --hard`、批量 delete。
* 遇到服务异常、dirty worktree、缺失 session、需要 repair/init 的情况，先向用户报告原因和建议命令。
* 只有用户明确确认后，才执行修复类或破坏性命令。

## 原则

* 单个 session 一律用 `session`。
* 多个 sessions 一律用 `sessions`。
* worktree 生命周期一律用 `pool`。
* 正常任务默认直接派发 `--yes`。
* 高风险任务才先预览派发。
* dispatch 会自动 idle-watch；主动查看结果只作为 fallback。
* 不确定命令时先执行 `python3 scripts/session-worktree-mgr.py -h` 或对应子命令 `-h`。
* 不要绕过 `pool prepare -> pool dispatch -> pool release` 生命周期。
* 修改前先确认 worktree、branch、session id 与 agent 对应关系。
