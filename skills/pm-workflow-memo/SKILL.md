---
name: pm-workflow-memo
description: PM 专用：Workflow M(emo) 记忆保存（勿忘）。从对话提炼要点 → 路由到 project_memory / user_profile → 按格式维护。
---

# pm-workflow-memo

PM 专用 skill——记忆保存。入口：直接执行本文步骤；不需 shell 脚本。

## 触发规则

| 触发器 | 行为 |
|---|---|
| 任意模式下用户说 `勿忘` | 启动本工作流 |

`勿忘` 的两种使用方式：

**方式一：附带内容**

- `勿忘，XXXX` — 将 XXXX 作为需要记住的信息
- 示例：`勿忘，我周末不写代码` → 保存为协作偏好

**方式二：指代上文**

- 在对话中提及了重要信息后说 `勿忘` — 将上文的要点提炼保存
- 示例：闲聊中用户提到禁用 Typescript 的某特性 → `勿忘` → 保存到技术决策

## 语言要求

| 输出类型 | 语言 |
|---|---|
| 记忆文件 | **简体中文**（项目状态、用户偏好、决策记录） |
| SKILL 注释 | **简体中文** |

## 步骤

1. **提炼需要记住的信息**（从附带的文本或最近的对话中提取关键点）
2. **判断适合存入哪个记忆文件**：
   - `.pm/project_memory.md` → 项目状态与约定（`## Operational Conventions`、`## Tech Decisions`、`## Recurring Project Context`）
   - `.pm/user_profile.md` → 用户的个人相关（工作习惯、生活偏好、性格观察、常聊话题、怪癖）
   - **不写入** `.pm/user_behavior.md`（仅模式切换钩子写入）
   - **不写入** `chats/`（仅闲聊过程写入）
3. **写入对应文件**，按各自格式维护规则处理
4. **回复简洁确认**（闲聊模式下可以稍带温度）

## 回复风格

- **管理模式**：`记住了，已更新到 pm_memory。`
- **闲聊模式**：更轻松的确认，例如 `记下了✨` / `收到，已刻进我的小本本📝` 等

## 关键边界

- **不写入** `user_behavior.md`（仅模式切换钩子写入）
- **不写入** `chats/`（仅闲聊过程写入）
- 不存储敏感个人信息
- 入 project_memory 前遵守 [Communication Memory Format](../../../.opencode/agents/project-manager.md#communication-memory-format) 的覆盖规则（Archived 超 5 条/30 天 → 压缩为月度摘要）
