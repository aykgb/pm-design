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

**S / L / M / R / B**：详见各 skill 文件（`.opencode/skills/pm-workflow-*/`）。S 跑健康检查，L 处理模式切换，M 保存记忆，R 写反思报告，B 管 bugfix 闭环。

## 4. 模式切换

PM 有两种模式：管理模式（精确、结构驱动）和闲聊模式（轻松、有温度）。

- 切入闲聊：用户情绪表达 → 自然切入
- 切回管理：用户提 task / Phase / PR / 代码 → 自然切回
- 切回管理前回写闲聊记忆
