# Agent System

> 通用 PM 调度系统的 Agent 体系设计——角色定义、派发模型、协作协议。

---

## 1. 角色表

| 角色 | 方式 | 职责 | 一句话 |
| --- | --- | --- | --- |
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

## 2. 两大派发模型

```
业务 Agent（pool worktree，串行）→ 详见 runtime-architecture.md
PM 域 Agent（main session，按需）→ 详见 runtime-architecture.md
工具 Agent（subagent，fire-and-forget）
```

> 完整运行时架构包括异步调度、idle-watch、worktree 生命周期，见 `runtime-architecture.md`。

## 3. 协作协议（速查卡）

| 交互 | 派发格式 | 产出 | 不做什么 |
| --- | --- | --- | --- |
| PM → 开发 | task ID（"做 P3-T1"） | commit hash + PR URL | 不替 agent 做设计决策 |
| PM → 审查 | "review P<N>-T<M>" | P0/P1/P2 报告 | 不修代码 |
| PM → QA | "验证 P<N>-T<M>" | 通过/失败 + 原因 | 不修代码 |
| PM → General | impl_plan §N + 必读文档清单 + 产出要求 | 定位报告 / spec 文件 | 不口述 spec 细节 |
| PM → 门禁 | "review P<N> 任务批次" | Blocker/High/Med/Low | 不改源码 |

**Fix 循环**：审查 P0/P1 → 开发 fix → 审查 R2。放行条件 `0 P0 AND 0 P1`。仅 P2 → APPROVE + Backlog。

**门禁规则**：PASS 后所有 findings 必须全修或显式降级。PASS ≠ 可跳过残余。
