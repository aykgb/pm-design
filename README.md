# PM System Kit

> 通用 PM 调度系统的设计文档、规格与模板——以 git submodule 分发，跨项目复用。

## 快速接入

```bash
# 1. 添加 submodule
git submodule add https://github.com/<org>/pm-design .pm/design

# 2. 编写项目配置
vim pm.config.yaml

# 3. Bootstrap
.pm/design/bootstrap          # 复制 templates/ 到项目对应位置

# 4. 开始开发
# 开发者说 "下一步"，PM 启动 Workflow N
```

## 目录

```
.pm/design/
  README.md                     ← 本文件
  pm_system_design.md           ← 总纲（架构全景图 + 子系统索引）
  adr/                          ← 架构决策记录（按编号）
  specs/                        ← 子系统详细规格
    agent-system.md             ← Agent 体系
    pipeline-system.md          ← 两条流水线
    spec-breakdown.md           ← Spec 拆解 + Batch 设计
    workflow-system.md          ← Workflow 路由
    runtime-architecture.md     ← 运行时引擎 + 自愈
    file-conventions.md         ← 文件体系 + 约定
  templates/                    ← 骨架模板（含占位符，bootstrap 时复制）
    pm/                         ← .pm/ 域模板
    agents/                     ← Agent 定义模板
```

## 设计原则

- **项目无关**：本目录不含任何具体项目的命名、路径、业务逻辑
- **版本可追溯**：每个设计决策以 ADR 形式记录在 `adr/`
- **模板含占位符**：`<agent-name>` / `<项目名>` 等，bootstrap 时从 `pm.config.yaml` 渲染
- **Skills 不放此处**：`pm-workflow-*` skills 是 PM 运行时，随项目 `.opencode/` 走

## 变更记录

| Date | Author | Change |
|------|--------|--------|
| 2026-06-13 | PM | 首版：Submodule 结构 + 6 个 spec + 6 个 template + 总纲索引 |
