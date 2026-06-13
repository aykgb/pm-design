# PM System Kit

> 基于 **OpenCode + Worktree Pool + 多 Agent 异步协作** 的通用 PM 调度系统——以 git submodule 分发，跨项目复用。

## 架构图

[![PM System Kit Architecture](diagrams/pm-system-architecture.png)](diagrams/pm-system-architecture.html)

> 点击图片在新窗口打开交互式架构图（支持 📋 Copy · 🖼️ PNG · 📄 PDF 导出）。

## 核心能力

### 多 Agent 异步协作

基于 OpenCode session + prompt_async API 构建的异步 agent 池体系：

- **10 agent 类型**：Daedalus (后端) · Morpheus (前端) · Themis (审查) · QA (测试) · Momus (门禁) · Clio (文档审查) · Janitor (杂务) · General (综合) · explore (探索) · WebSearch (外搜)
- **独立 worktree 隔离**：每个 worktree 绑定独立 git branch，10 个固定 worktree 池，互不干扰
- **7 步标准流水线**：pool prepare → Daedalus/Morpheus → Themis → Codex → QA → merge → pool release
- **Worktree / Main 双路径**：worktree agent 走 `pool dispatch`，main agent (Janitor/Momus/Clio) 走 `session dispatch`
- 详见 [`specs/pipeline-system.md`](specs/pipeline-system.md)

### `overview` — 全局 Session 状态一览

一条命令看清整个多 agent 系统的运行状态：

```bash
python3 scripts/session-worktree-mgr.py overview
```

输出内容：

- **服务健康**：OpenCode Server + Session Sidecar 是否 healthy
- **Worktree 表格**：每个 wt 的 branch / commit / dirty / Δmain / agent 列表
- **Session 详情**：每个 agent session 的 Input / Output+Reasoning / Cache.Read 令牌量 / Cache Hit% / Context 剩余 / 最后更新 / 当前状态 (idle/busy/streaming)
- **PM 分组**：main worktree sessions 按 PM 会话隔离，当前 session 置顶
- **Stuck 标记**：busy/streaming > 15min 且无活动更新的 session 标注 `[STUCK]`

无需人工逐个检查 session 状态——overview 一次拉取全部 session 信息（1 次 HTTP 全局 `/session?limit=2000` + 1 次 sidecar `/status`）。

### Stuck 检测 & 自动恢复

- **Stuck 检测**：idle-watch 守护进程在 dispatch 时自动启动，轮询 sidecar status，session busy > 15min 且 `time.updated` 无刷新 → 发送 `[stuck-notify]` 到 PM session
- **自动恢复**：`pool dispatch --force` 对 busy+stale 的 session 自动 hard-delete + 重建，无间断恢复派发
- **Stale 清理**：`pool release` 自动归档 >1d 的旧 session、清理 tombstoned session ID
- [`specs/runtime-architecture.md`](specs/runtime-architecture.md) §自愈

### PM Agent 多规则文件注入

PM agent 启动时自动注入多规则文件，实现跨 session 的行为一致性：

| 文件 | 作用 |
|------|------|
| `persona.md` | 管理模式 / 闲聊模式双模式人格、语气边界、根因分析优先 |
| `operational_conventions.md` | 5 组操作约定 (OC0 优先级 · OC1 沟通 · OC2 分支 · OC3 派发 · OC4 模式 · OC5 开发) |
| `project_memory.md` | 交互历史、agent 派发模板、项目决策、Workflow 路由 |
| `user_profile.md` | 开发者偏好、习惯、性格画像 |
| `user_behavior.md` | 行为日志、模式切换频率、使用摘要 |

注入方式：`pm-guardian.js`（OpenCode 插件）启动时读取 `plugins/pm-guardian.conf.json` 中的 `instructFiles` 列表，将指定文件注入 PM agent 的 system prompt，实现跨 session 规则一致性。不依赖 OpenCode 原生 `instructions` 配置。

```
// plugins/pm-guardian.conf.json
{
  "targetAgent": "pm",
  "instructFiles": [
    "persona.md",
    "session_worktree_mgmt.md",
    "operational_conventions.md"
  ]
}
```

## 快速接入

```bash
# 1. 添加 submodule
git submodule add https://github.com/aykgb/pm-design .pm/design

# 2. Bootstrap（复制 skills / runtime / templates 到项目）
python .pm/design/scripts/pm-bootstrap.py

# 3. 配置 OpenCode agent
#    编辑 .opencode/opencode.json 添加 PM agent 定义

# 4. 开始开发
#    开发者说 "下一步"，PM 启动 Workflow N
```

## 目录

```
.pm/design/
  README.md                     ← 本文件
  diagrams/
    pm-system-architecture.html ← 交互式架构图（含导出功能）
    pm-system-architecture.png  ← 架构图截图
  pm_system_design.md           ← 总纲（架构全景图 + 子系统索引）
  adr/                          ← 架构决策记录（按编号）
  specs/                        ← 7 个子系统详细规格
    agent-system.md             ← Agent 体系
    pipeline-system.md          ← 两条流水线
    spec-breakdown.md           ← Spec 拆解 + Batch 设计
    workflow-system.md          ← Workflow 路由
    runtime-architecture.md     ← 运行时引擎 + 自愈
    worktree-session-mgmt.md    ← Worktree/Session 状态体系
    file-conventions.md         ← 文件体系 + 约定
  templates/                    ← 骨架模板（含占位符，bootstrap 时复制渲染）
    pm/                         ← 5 个 PM 域模板（CLAUDE/memory/conventions/persona/config）
    agents/                     ← 8 个 Agent 定义模板（PM/dev/review/QA/gate/doc/frontend/janitor）
  skills/                       ← 8 个 PM 工作流 skill（bootstrap 时复制到项目 .opencode/skills/）
  runtime/                      ← 4 个运行时组件（bootstrap 时复制到项目 scripts/）
    session-worktree-mgr.py     ← Worktree pool + session 管理引擎
    session-status-server.mjs   ← Session 状态 sidecar 服务
    check-codex.sh              ← Codex bot comments 拉取
    session-worktree-mgr.md     ← 命令速查
  scripts/                      ← 工具脚本
    pm-bootstrap.py             ← Bootstrap 入口
  plugins/                      ← OpenCode 插件
    pm-guardian.js              ← Session 追踪 / idle-watch 守护
    pm-guardian.conf.json       ← 守护插件配置
```

## 设计原则

- **项目无关**：本目录不含任何具体项目的命名、路径、业务逻辑
- **版本可追溯**：每个设计决策以 ADR 形式记录在 `adr/`
- **模板含占位符**：`<agent-name>` / `<项目名>` 等，bootstrap 时从模板渲染
- **Skills 在此维护**：8 个 `pm-workflow-*` skill 在 `skills/` 目录管理，bootstrap 时复制到项目 `.opencode/skills/`
- **Runtime 在此维护**：`session-worktree-mgr.py` / `session-status-server.mjs` / `check-codex.sh` 在 `runtime/` 目录管理，bootstrap 时复制到项目 `scripts/`

## 变更记录

| Date | Author | Change |
|------|--------|--------|
| 2026-06-13 | PM | v0.2：补完 8 skills + 5 agent 模板 + 4 runtime 文件 + CLAUDE 模板；7 个 spec；README 重写加架构图 |
| 2026-06-13 | PM | 首版：Submodule 结构 + 6 个 spec + 6 个 template + 总纲索引 |
