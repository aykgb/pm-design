# PM System Design

> 通用 PM 调度系统——Agent 协作、开发流水线、Spec 拆解、Batch 编排、Workflow 路由、记忆体系。
>
> 位于 `.pm/design/`，PM 自身设计迭代在此沉淀。与具体项目解耦。

Last updated: 2026-06-13

---

## 0. Quick Start — 10 分钟引导

### 0.1 Bootstrap：最小文件集合

从模板项目复制以下文件到新项目根目录：

```
.pm/
  project_memory.md           ← §附录 A 骨架模板
  operational_conventions.md  ← §附录 B 骨架模板
  persona.md                  ← PM 人格定义
  user_profile.md             ← 空模板
  user_behavior.md            ← 空模板

.opencode/
  agents/
    project-manager.md        ← PM agent 定义（核心，需适配 §0.2）
    <dev-agent>.md            ← 开发 Agent 定义
    <review-agent>.md         ← 审查 Agent 定义
    <qa-agent>.md             ← QA Agent 定义

docs/
  implementation_plan.md      ← 项目 Phase 拆解（Source of Truth，手动编写）
  architecture.md             ← 系统架构（手动编写）
  data_model.md               ← 数据模型（手动编写）
  development_workflow.md     ← 流水线详细步骤
```

> 不使用 worktree/session pool 的项目可跳过 Session & Worktree 管理组件。最简模式：PM + General（main worktree）+ 开发者手动执行审查/测试。

### 0.2 Configure：项目适配清单

| 适配项 | 位置 | 说明 |
|--------|------|------|
| Agent 名称 | `.opencode/agents/*.md` `name:` 字段 | 给每个 Agent 起项目特定的名字 |
| CLAUDE.md / 项目宪法 | 项目根 | 定义安全规则、架构边界、禁止行为、文档读取约定 |
| `implementation_plan.md` | `docs/` | Phase 0→N 的任务拆分与验收标准 |
| 文档读取约定 | `CLAUDE.md` §必读文档 | 按任务类型（数据库/交易/API/前端）映射必读文档 |
| 风控/安全规则 | 业务相关 | 如有外部真实操作，定义不可绕过的安全链路（§10） |
| 健康检查项 | `pm-workflow-status/status.py` | 定义项目特定的 CI 健康度断言 |

### 0.3 Run：首次交互

```text
开发者: "下一步"
  → PM: Workflow N 启动 → 读 implementation_plan.md
  → 输出路径选项 → 开发者选方向
  → Spec 拆解（General 创建 task_specs/）
  → Batch 编组（PM 按 §4 标准）
  → 开发者: "开工"
  → PM: pool prepare → dispatch 开发 Agent → 审查 → QA → merge → 收口
```

### 0.4 核心流程图

```
┌─────────────────────────────────────────────────────┐
│                  PM 调度循环                         │
│                                                      │
│  impl_plan.md ──→ Spec 拆解 ──→ Batch 编组          │
│       │               │              │               │
│       │          General 执行    PM 按标准决策       │
│       │               │              │               │
│       ▼               ▼              ▼               │
│  project_tasks.md  ◀── task_specs/  + 执行计划       │
│       │                                              │
│       ▼                                              │
│  ┌─ 开发 Agent ─→ 审查 Agent ─→ QA Agent ─┐        │
│  │         (同 worktree，串行)              │        │
│  └────────── fix 循环 ← ──────────────────┘        │
│       │                                              │
│       ▼                                              │
│  开发者 merge PR → PM 收口 → devlog 更新             │
│       │                                              │
│       ▼                                              │
│  循环（下一 Batch 或下一 Phase）                     │
└─────────────────────────────────────────────────────┘
```

### 0.5 两种接入路径

| | 手工路径（§0.1–§0.3） | 程序化路径 |
|---|---|---|
| **适合** | 首次学习、理解内部结构、需要高度定制 | 快速启动新项目、标准化团队 |
| **命令** | 手工复制文件 + 编辑配置 | `pm bootstrap --from docs/` |
| **时间** | ~30 分钟 | ~2 分钟 |
| **原理** | 手动对照清单逐文件创建 | CLI 扫描 impl_plan.md + architecture.md → 自动生成全部 PM 域文件 |

```bash
# 程序化路径（3 步）
pm bootstrap --from docs/       # 扫描项目文档 → 生成 .pm/ + .opencode/ + project_tasks.md
pm config set agent.dev Daedalus # 设置 Agent 名（或编辑 pm.config.yaml）
pm start                         # 启动 OpenCode + PM agent → 等待 "下一步"
```

`pm.config.yaml`（自动生成，手动微调）：

```yaml
project:
  name: "Xidi Minimal"
  language: zh-CN

agents:
  dev: <dev-agent>       # 后端开发 Agent 名
  review: <review-agent> # 审查 Agent 名
  qa: <qa-agent>         # QA Agent 名
  gate: <gate-agent>     # 可选：Spec 门禁

conventions:
  branch_prefix: feat_P
  review_required: true
  tools_dir: scripts/

phases:
  source: docs/implementation_plan.md
```

> **Bootstrap vs 自愈**：`pm bootstrap` 生成初始骨架（.pm/ + .opencode/ + project_tasks.md），但不追求"一次性完美"——它只创建最小可用集。剩余能力（审查 Agent、Spec 门禁、前端 Agent 等）由 §12 迭代自愈在开发中按需补全。两个机制互补：bootstrap 解决"从零到一"，自愈解决"从一到全"。

---

## 1. Agent 体系

### 1.1 角色表

| 角色 | 方式 | 职责 | 一句话 |
|------|------|------|--------|
| **PM** | — | 总调度，任务状态唯一维护者，不读不写业务代码 | "项目到哪了，下一步做什么" |
| **开发 Agent** | pool wt | 后端/前端实现，commit+push+create PR | "按 spec 写代码，建 PR" |
| **审查 Agent** | 同 wt | 代码审查，P0/P1 零容忍 | "这代码能合吗" |
| **QA Agent** | 同 wt | lint + type-check + test | "测试过了吗" |
| **General** | main session | 工具链修复 / Spec 拆解，PM 显式给 workflow | "定位这个 bug，拆这个 Phase" |
| **Spec 门禁** | main session | Phase 任务 spec 审视，派发前强制通过 | "这个 spec 能派吗" |
| **文档审查** | main session | 文档漂移/矛盾/过时引用 | "文档和代码一致吗" |
| **杂务 Agent** | subagent | git/清理/整理，禁 merge | "提交这个，清理那个" |
| **探索 Agent** | subagent | 只读代码库搜索 | "这个函数在哪" |
| **搜索 Agent** | subagent | 外部网页搜索 | "查一下这个库的文档" |

### 1.2 两大派发模型

```
业务 Agent（pool worktree，串行）→ 详见 §10.2
PM 域 Agent（main session，按需）→ 详见 §10.2
工具 Agent（subagent，fire-and-forget）
```

> 完整运行时架构包括异步调度、idle-watch、worktree 生命周期，见 §10。

### 1.3 协作协议（速查卡）

| 交互 | 派发格式 | 产出 | 不做什么 |
|------|---------|------|---------|
| PM → 开发 | task ID（"做 P3-T1"） | commit hash + PR URL | 不替 agent 做设计决策 |
| PM → 审查 | "review P<N>-T<M>" | P0/P1/P2 报告 | 不修代码 |
| PM → QA | "验证 P<N>-T<M>" | 通过/失败 + 原因 | 不修代码 |
| PM → General | impl_plan §N + 必读文档清单 + 产出要求 | 定位报告 / spec 文件 | 不口述 spec 细节 |
| PM → 门禁 | "review P<N> 任务批次" | Blocker/High/Med/Low | 不改源码 |

**Fix 循环**：审查 P0/P1 → 开发 fix → 审查 R2。放行条件 `0 P0 AND 0 P1`。仅 P2 → APPROVE + Backlog。

**门禁规则**：PASS 后所有 findings 必须全修或显式降级。PASS ≠ 可跳过残余。

---

## 2. 两条流水线

### 2.1 选择速查

| 改动 | 流程 | worktree | 审查 |
|------|------|----------|------|
| 业务代码 | **标准 7 步** | pool wt | 审查 Agent + CI Bot |
| 工具链代码 | **工具链闭环**（General 一人） | main wt | 跳过 |
| PM 域文档 | PM 直推 main | main wt | 跳过 |
| Bugfix | 快速修复流程 | per-fix wt | 可选 |

> **裁减**：开发者可按复杂度跳过审查/QA/CI Bot 步骤。

### 2.2 标准 7 步

```
① pool prepare → ② 开发 Agent → ③ 审查 Agent → ③.5 CI Bot
  → ④ QA Agent → ⑤ 开发者 merge → ⑥ pool release → ⑦ PM 收口
```

**3 个铁律**：同 wt 串行不 release / 审查 P0/P1 零容忍 / PM 禁止 merge。

详细步骤 + 失败回滚表见 `development_workflow.md`。

### 2.3 工具链闭环

General 一人定位→修复→验证→PR。≤30 行 cherry-pick 直推，>30 行 PR。自验标准：工具可运行、核心命令不崩、幂等、向后兼容。

---

## 3. Spec 拆解

### 3.1 端到端流程

```
Phase 目标（impl_plan.md §N）
  → ≤2 task: PM 直接建 spec
  → ≥3 task: PM 派 General 拆解（给 impl_plan §N + 必读文档清单）
  → General 读源码 grep 验证 → 产出 task_specs/P<N>-T<M>.md + 代码预估
  → PM 核验 spec 逻辑自洽性
  → PM 编组 Batch（按 §4 标准）
  → Spec 门禁审视（正式 Phase 任务强制）
  → Fix loop → PASS → spec 就位，可派发
```

### 3.2 Spec 文件规范

每个 `task_specs/P<N>-T<M>.md`：

| 字段 | 要求 |
|------|------|
| Goal | 一句话：实现什么、为什么现在做 |
| Steps | 可执行步骤，含文件路径和函数名（General 从源码验证） |
| Acceptance | 可验证条件（测试通过 / lint 全绿 / 具体行为断言） |
| Related files | 涉及文件清单 |
| 行号快照 | 时间戳 + "实施时用 grep 找位置" |
| 代码预估 | 净增行数 + 测试数 |

### 3.3 PM 核验（不读代码）

- Goal 与 impl_plan.md 对齐
- Steps 依赖链完整（T<N> 产出 → T<N+1> 消费）
- Acceptance 可验证（非口号）
- 代码量在 Batch 阈值内

---

## 4. Batch 设计标准

> 一个 Batch = 一个 PR。

### 合并（何时合并）

| 优先级 | 原则 | 判断 |
|--------|------|------|
| 1 | 同文件 / 同 causal chain | 逻辑首尾相连 → 必须合并 |
| 2 | 共享测试边界 | 同组 fixture/mock → 合并 |
| 3 | 串行依赖 | T<N+1> 依赖 T<N> 接口 → 合并 |

### 拆分（何时独立）

| 优先级 | 原则 | 判断 |
|--------|------|------|
| 1 | 不同文件 / 不同 chain | 完全不交叠 → 可拆分 |
| 2 | 独立可测 | 可独立验证 → 可拆分 |
| 3 | 风险隔离 | 安全边界/核心逻辑 → 独立 PR |

### 阈值

| 指标 | 上限 | 理由 |
|------|------|------|
| 单 Batch 行数 | ~2,000 | 超过审查疲劳，检出率降 |
| Batch task 数 | 3–7 | 与 Task Selection 对齐 |
| Phase PR 数 | 2–5 | 过多流水线开销；过少 fix loop 代价高 |

### 反模式

- **过度拆分**：5+ PR 每个 <500 行 → 流水线开销主导
- **过度合并**：1 PR >3,000 行 → fix loop 爆炸半径大
- **跨文件假内聚**：因"同属一个 Phase"合并不相关文件 → 审查遗漏

> 裁决权归开发者。

---

## 5. PM Workflow

### 5.1 一览

| ID | 触发 | 功能 |
|----|------|------|
| I | 首次调用 | 项目初始化：创建 task/devlog/记忆 |
| S | `status` | 健康检查 + 漂移报告 |
| N | `下一步` | 路径选项 → Spec 拆解 → 门禁 |
| F | `finish` / merge | 收口：devlog + task 迁移 |
| L | 闲聊 | 模式切换 |
| M | `勿忘` | 记忆保存 |
| R | `反思` | 审计报告 |
| B | bug 报告 | 快速 bugfix |

### 5.2 核心链

```
"下一步" → N（路径→拆解→门禁→就绪）
  → "开工" → 开发→审查→QA→merge
  → F（收口）→ 循环
```

### 5.3 关键 Workflow

**N — Next**：读 memory + tasks → 输出最多 3 条路径 → 用户选 → 拆 spec → 门禁 → "Batch 就绪"

**F — Finish**：读 tasks → 检测完成项 → 脚本收口（task 迁移 + devlog）→ 审查发现回填 Backlog → memory 同步

**S / L / M / R / B**：详见各 skill 文件（`.opencode/skills/pm-workflow-*/`）。S 跑健康检查，L 处理模式切换，M 保存记忆，R 写反思报告，B 管 bugfix 闭环。

**模式切换**：切回管理前回写闲聊记忆。

---

## 6. Task 状态机

```
Todo → In Progress → Done → Recently Completed
```

| 状态 | 含义 | 触发 |
|------|------|------|
| Todo | spec 就位待派发 | 门禁 PASS |
| In Progress | 流水线进行中 | pool dispatch |
| Done | PR merged 待收口 | 开发者 merge |
| Recently Completed | 收口完成 | 收口脚本 |

任务类型：`P<N>-T<M>`（代码，走门禁+7 步）/ `P<N>-e2e`（集成测试）/ `BL-*`（清扫，不走门禁）。

---

## 7. 提交与分支规范

### 7.1 权限矩阵

| 谁 | 域 | 方式 |
|----|-----|------|
| PM | 记忆/文档域 | 直推 main |
| 开发 Agent | `src/` `tests/` `db/` | PR |
| General | 工具链代码 | fix/feat 分支 → PR |
| 任何人 | 破坏性操作 | 先征询 |

### 7.2 分支与 Commit

- 分支：`feat_P<N>_T<M>_<task>` / `fix-<slug>`
- Commit：`<type>(<scope>): [<agent>] <description>`
- main 只做文档增删改；PM 禁止 merge

---

## 8. Backlog 管理

来源：审查 P2 / 门禁 Med+Low / CI Bot P2 / QA 非阻断。
不进入：typo / 单行 Nit / PM 裁决忽略。
清扫：关/修/留三分类 → QA Agent 执行。原则：低优全量追踪。

---

## 9. PM 域文件体系

```
.pm/                          ← 记忆系统
  project_memory.md           ← 核心：Agent 派发 · 拆解 · Batch · 交互历史
  operational_conventions.md  ← 操作约定
  persona.md                  ← PM 语气
  user_profile.md             ← 用户画像
  user_behavior.md            ← 行为日志
  chats/ + reflections/       ← 闲聊 + 反思

.opencode/                    ← Agent 定义 + Skills
  agents/*.md                 ← N 个 agent 定义
  skills/pm-workflow-*/       ← I/S/N/F/L/M/R/B 工作流

docs/                         ← 项目文档
  impl_plan.md                ← Phase 拆解（唯一 Source of Truth）
  architecture.md + data_model.md
  .pm/design/pm_system_design.md ← 本文
  development_workflow.md     ← 流水线权威
  project_tasks.md            ← 任务状态索引
  development_log.md          ← 历史单表
  task_specs/ + review_report/
```

### 角色矩阵

| 场景 | 读 | 写 |
|------|-----|-----|
| PM 派发 | memory §Agent 派发 | — |
| PM 拆 spec | memory §Spec 拆解 | 派 General 写 task_specs |
| PM 收口 | — | 收口脚本 --devlog |
| Agent 执行 | tasks + spec + workflow | — |

---

## 10. 外部系统边界

> 适用于依赖硬件 SDK、第三方 API 的项目。

### 隔离模式

```
开发环境（mock）          生产环境（real）
  mock 注入 + lazy import    真实 SDK 完整连接
        ↓                       ↓
  ┌──────────────────────────────────┐
  │        业务逻辑（不变）           │
  └──────────────────────────────────┘
```

### 规则

| 规则 | 说明 |
|------|------|
| lazy import | SDK 在函数内 import，开发环境无 SDK 不崩溃 |
| mock 注入 | 参数注入 mock，生产注入真实 |
| 进程隔离 | SDK 限定在独立 worker，不与 API Server 共享 |
| 安全默认 | 开发默认 dry-run/禁止真实调用，生产显式开启 |

### 不可绕过的安全链路

如项目涉及对外真实操作，必须定义：

```
用户意图 → 风控检查 → 操作执行 → 回调确认 → 状态更新
```

任何路径不得跳过。风控结果可审计。

---

## 11. 运行时架构

> PM 系统不只是一套文档——它是一个基于 OpenCode + worktree/session pool 的**异步调度引擎**。

### 10.1 引擎分层

```
┌──────────────────────────────────────────────────────────┐
│                    pm CLI                                 │
│  pm init    pm bootstrap    pm status    pm dispatch      │
└────────────┬─────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────┐
│              OpenCode Runtime Engine                      │
│                                                           │
│  ┌─────────────────┐  ┌──────────────────┐               │
│  │ worktree pool   │  │  session manager  │               │
│  │ (N× git wt)     │  │  (lifecycle +     │               │
│  │ prepare/release │  │   idle-watch)     │               │
│  └────────┬────────┘  └────────┬─────────┘               │
│           │                    │                          │
│           ▼                    ▼                          │
│  ┌────────────────────────────────────────┐              │
│  │         Agent Dispatch Layer            │              │
│  │                                         │              │
│  │  pool dispatch wt_N <dev-agent> --task "..." --yes      │
│  │  session dispatch <sid> General                       │
│  │  task(subagent_type="Janitor")                        │
│  └────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────┐
│              PM Agent (Orchestrator)                      │
│                                                           │
│  Skills → Workflows → Dispatch → Idle-watch → 收口       │
│  Memory (.pm/) → project_tasks.md → devlog               │
└──────────────────────────────────────────────────────────┘
```

### 10.2 核心能力：异步调度

PM 不"等" Agent 完成——派发后 Agent 在独立 worktree 异步工作：

```
PM: "做 P<N>-T<M>" → dispatch <dev-agent> (wt_1)
  → <dev-agent> 在 wt_1 异步工作（读 spec → 写代码 → commit → create PR）
  → <dev-agent> busy → idle
  → idle-watch 通知 PM session
  → PM 自动推进: dispatch Themis (同 wt_1) → dispatch QA → 汇报 merge
```

**开发者在流程中的参与点只有三个**：说"下一步"选方向、说"开工"批执行、merge PR。其余全部由 PM 自动调度。

### 10.3 Worktree Pool 管理

```
idle wt_1  → prepare → checkout feat_xxx → busy
  → Daedalus session 启动 → busy
  → Daedalus idle → Themis session 启动 → busy
  → Themis idle → QA session 启动 → busy
  → QA idle → PR merged → release → idle
```

**会话复用**：审查和 QA Agent 复用同一个 worktree 和同一个 session，无需重新 prepare。确保它们看到的代码与开发 Agent 一致。

**资源泄漏防护**：任何时候操作 worktree 后必须显式 release。idle-watch 是兜底——agent 异常退出时 PM 可感知并释放。

### 10.4 最简模式（无 Pool）

如果项目不使用 worktree pool：

```
main worktree
  → PM + General（main session）
  → 开发者在自己的 IDE 中手动开发
  → PM 仍调度 Spec 拆解、Batch 编排、门禁审视
  → 审查/测试由开发者手动执行或 CI 自动跑
```

此时 PM 的角色从"全自动调度器"退化为"结构化引导者"——仍提供 I/S/N/F/L/M/R 工作流，但依赖 pool 的 Workflow（B bugfix 的 per-fix wt）不可用，审查/测试由开发者手动执行或 CI 自动跑。

---

## 12. 迭代自愈

> PM 系统在开发迭代中自动检测缺失能力并补全——开发者不需要停下业务来"完善 PM 工具链"。

### 11.1 自愈矩阵

| 检测点 | 缺失 | PM 自动动作 |
|--------|------|------------|
| Workflow N | `task_specs/` 目录为空 | 自动派 General 拆解 spec |
| Workflow N | 审查 Agent 未定义 | 从 `agents/review.agent.md` 模板渲染 → 提示开发者确认后创建 |
| Workflow N | 缺少 `frontend_spec.md` 但 Phase 涉及前端 | 提示："建议先编写 frontend_spec.md" |
| Workflow S | 健康检查项过时 | 自动更新 `status.py` 配置 |
| Workflow F | `devlog` 表不存在 | 自动创建空表 |
| 首次 dispatch | worktree pool 未初始化 | 检测 → 提示 `pool init --size 10` |
| Phase 收口 | 下一 Phase spec 未拆 | 自动触发 Spec 拆解（无需开发者说"下一步"） |

### 11.2 渐进式能力升级

```
Phase 1 启动:
  PM 检测: 有 impl_plan.md → 自动拆 spec → 门禁审视
  PM 检测: 无审查 Agent → 提示创建
  开发者: "跳过审查，直接 QA"（裁减模式）

Phase 3 启动:
  PM 检测: 代码量变大 → 建议启用审查 Agent
  PM 检测: 涉及外部 SDK → 建议启用 §9 安全边界检查
  开发者: "好，启用审查" → PM 从模板创建审查 Agent 定义

Phase 6 启动:
  PM: 前端 Agent 未定义 → 从模板创建 <frontend-agent>
  PM: frontend_spec.md 已编写 → 自动拆 spec
```

### 11.3 设计原则

- **默认最小，按需生长**：新项目从最简模式（PM + General）开始，PM 在迭代中逐项提示并补全
- **检测优于询问**：PM 主动检测缺失，不给开发者开问题清单
- **自动优于手动**：能自动创建的（devlog 表、task_specs 目录、status 配置）一律自动创建
- **危险操作必确认**：创建 Agent 定义文件、修改 CI 配置、启用安全边界——这些必须先征询开发者

---

## 附录 A：`project_memory.md` 骨架

```markdown
# Project Memory

项目协作记忆。

> Persona → persona.md | 计划 → impl_plan.md + project_tasks.md

Last update: YYYY-MM-DD

## Recurring Project Context

- **项目**：<项目名>
- **当前 Phase**：Phase N — <描述>
- **Tests**：<N>

### 实用命令速查
（项目特定命令）

## Agent 派发

### 业务 Agent
（Agent 名称 + 派发方式 + 约束）

### PM 域执行 Agent
（General / 门禁 / 文档审查 / 杂务）

### 派发约定
（流水线 / 派发前置 / PM 禁止 merge / 破坏性操作确认）

## Spec 拆解流程
（引用 .pm/design/pm_system_design.md §3）

## Batch 设计标准
（引用 .pm/design/pm_system_design.md §4）

## OC 要点
（引用 operational_conventions.md）

## Interaction History
| 日期 | 摘要 |
|------|------|
```

---

## 附录 B：`operational_conventions.md` 骨架

```markdown
# Operational Conventions

## OC0. 约定优先级
项目约定优先于用户单次 Prompt。冲突时指出、等确认。

## OC1. 沟通
- 中文，术语保留原文
- 时间戳查系统时间
- 文件路径用项目根相对路径
- 先想后动

## OC2. 分支与提交
- main 只做文档增删改
- 谁改谁提交（例外：开发者说"直接提交"）
- 分支命名 feat_P<N>_T<M>_<task>
- commit 格式 <type>(<scope>): [<agent>] <description>
- PM 禁止 merge/rebase/cherry-pick

## OC3. Agent 派发
- 体系：业务（pool wt）+ PM 域（main session）+ 工具（subagent）
- 委派：业务 Agent 只给 task ID；PM 域 Agent 给目标+范围
- 低优发现全量追踪入 Backlog
- 门禁 PASS 后所有 findings 全修或显式降级

## OC4. 模式切换
切回管理前回写闲聊记忆

## OC5. 开发规范
- wt 生命周期：操作后必须 release
- 标准 7 步：prepare→开发→审查→QA→merge→release→收口
- 工具链代码：General 闭环
- devlog 范围：代码收口 + Phase 收口
- 派发前置：先 push spec 到 main 再 prepare
- 流程裁减：开发者可按需跳过步骤
- 破坏性操作先征询
```

---

## 变更记录

| Date | Author | Change |
|------|--------|--------|
| 2026-06-13 | PM | 迁入 `.pm/design/`——PM 自身设计迭代在此沉淀 |
| 2026-06-13 | PM | v3：加 §0.5 程序化路径、§11 运行时架构（OpenCode 引擎 + 异步调度）、§12 迭代自愈矩阵 |
| 2026-06-13 | PM | v2：重写为引导式结构，加 Quick Start、附录骨架模板、Core Flow Diagram。删 30% 冗余 |
| 2026-06-13 | PM | 首版：Agent 体系 + 流水线 + Spec 拆解 + Batch + Workflow + 文件体系 |
