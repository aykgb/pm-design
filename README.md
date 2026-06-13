# PM System Kit

> 通用 PM 调度系统的设计文档、规格与模板——以 git submodule 分发，跨项目复用。

## 快速接入

```bash
# 1. 添加 submodule
git submodule add https://github.com/<org>/pm-design .pm/design

# 2. 编写项目配置（可选，默认值可用）
vim pm.config.yaml

# 3. Bootstrap
python .pm/design/scripts/pm-bootstrap.py --from docs/

# 4. 开始开发
# 开发者说 "下一步"，PM 启动 Workflow N
```

## 目录

```
.pm/design/
  README.md                     ← 本文件
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
- **模板含占位符**：`<agent-name>` / `<项目名>` 等，bootstrap 时从 `pm.config.yaml` 渲染
- **Skills 在此维护**：8 个 `pm-workflow-*` skill 在 `skills/` 目录管理，bootstrap 时复制到项目 `.opencode/skills/`

## 变更记录

| Date | Author | Change |
|------|--------|--------|
| 2026-06-13 | PM | 补完：8 skills + 5 agent 模板 + 4 runtime 文件 + CLAUDE 模板；7 个 spec |
| 2026-06-13 | PM | 首版：Submodule 结构 + 6 个 spec + 6 个 template + 总纲索引 |
