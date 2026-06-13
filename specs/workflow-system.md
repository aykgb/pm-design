# Workflow System

> PM 工作流体系——8 个 Workflow 的触发、功能和核心逻辑。

---

## 1. 一览

| ID | 触发 | 功能 |
| --- | --- | --- |
| I | 首次调用 | 项目初始化：创建 task/devlog/记忆 |
| S | `status` | 健康检查 + 漂移报告 |
| N | `下一步` | 路径选项 → Spec 拆解 → 门禁 |
| F | `finish` / merge | 收口：devlog + task 迁移 |
| L | 闲聊 | 模式切换 |
| M | `勿忘` | 记忆保存 |
| R | `反思` | 审计报告 |
| B | bug 报告 | 快速 bugfix |

## 2. 核心链

```
"下一步" → N（路径→拆解→门禁→就绪）
  → "开工" → 开发→审查→QA→merge
  → F（收口）→ 循环
```

## 3. 关键 Workflow

**N — Next**：读 memory + tasks → 输出最多 3 条路径 → 用户选 → 拆 spec → 门禁 → "Batch 就绪"

**F — Finish**：读 tasks → 检测完成项 → 脚本收口（task 迁移 + devlog）→ 审查发现回填 Backlog → memory 同步

**S / L / M / B**：详见各 skill 文件（`.opencode/skills/pm-workflow-*/`）。S 跑健康检查，L 处理模式切换，M 保存记忆，B 管 bugfix 闭环。

### R — Reflection

`反思` 触发。按当前模式（管理/闲聊）执行不同的审计路径。

**管理模式下 8 步**：

1. 审计 `project_memory.md` — 约定遵循、Phase 状态、技术决策
2. 审计 `development_log.md` — 近期失败未跟进、决策未记录
3. 工作流触发统计 — Status/Next/Finish/Reflection/Leisure/Check/Memo/Deploy/Bug 各几次；OC5 管道闭环次数
4. **对话轮次统计** — 逐轮列出（# / 用户发言 / PM 动作 / 类型）；分析模式（连续同类型？反复改同一文件？长时间等 agent？）
5. 反思报告 — 偏差/漂移、Prompt 遵循、动作复盘、文件读写优化、任务编排、context 传递、agent 行为、OC 约定
6. 会话总结 — 按模块列出关键动作 + 产出 + 关键数字
7. 可固化工作流 — 候选列表供开发者裁决
8. 落盘 `.pm/reflections/YYYY-MM-DD_HH-MM_session.md`

**闲聊模式下 4 步**：审计 user_profile → 审计 chats/INDEX → 输出反思 → 落盘。

**报告模板**含：会话总结 / 偏差漂移 / 工作流触发统计 / 对话轮次统计 / Agent 行为审计 / 文件读写优化 / 可固化工作流 / OC 约定审查。

## 4. 模式切换

PM 有两种模式：管理模式（精确、结构驱动）和闲聊模式（轻松、有温度）。

- 切入闲聊：用户情绪表达 → 自然切入
- 切回管理：用户提 task / Phase / PR / 代码 → 自然切回
- 切回管理前回写闲聊记忆
