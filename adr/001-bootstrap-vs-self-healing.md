# ADR-001: Bootstrap vs Self-Healing 策略

> 状态：Accepted  
> 日期：2026-06-13  
> 决策者：PM

## 背景

PM 系统有两类文件需要创建：

1. **结构文件**：project_tasks.md、agent 定义、skill 文件——依赖项目上下文（implementation_plan.md 的 Phase 拆分、项目技术栈等），无法用纯默认值生成。
2. **独立文件**：project_memory.md、pm.config.yaml、operational_conventions.md——可从模板 + 默认值生成，不依赖项目上下文或依赖极少。

两种创建策略：

| 策略 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **Bootstrap** | `pm-bootstrap.py` 一次性预置所有文件 | 干净启动、模板确保一致性 | 需要 submodule 就位、需要执行步骤 |
| **Self-Healing** | 每个 Workflow 启动时检查 → 缺失则按需创建 | 零配置、容错自动恢复 | 碎片化创建、不同 Workflow 创建的格式可能不一致 |

## 决策

采用**混合策略**：Bootstrap 为主路径，Self-Healing 为兜底容错。

```
新项目 ──▶ Workflow I ──▶ 尝试 bootstrap（路径 A）
                │               │
                │          ┌─────┴─────┐
                │          │ submodule  │  就位？──▶ yes ──▶ pm-bootstrap.py ──▶ 一次性生成全部
                │          │ 就位？     │
                │          └─────┬─────┘
                │               │ no
                │               ▼
                └────── 降级路径 B：手工逐个创建
                
运行时 ──▶ 任意 Workflow ──▶ 检查关键文件存在？
                                    │
                               ┌────┴────┐
                               │ 缺失     │ 存在
                               ▼          ▼
                          触发 Workflow I   继续执行
```

### 文件分层

| 层级 | 文件类型 | 创建方式 | 示例 |
|------|----------|----------|------|
| **L1 必须预置** | 依赖项目上下文的文件 | Bootstrap 创建 | project_tasks.md（需读 impl_plan）、agent 定义（需项目名+技术栈）、skill 文件 |
| **L2 可预置可自愈** | 模板驱动的独立文件 | Bootstrap 创建 + 运行时自愈兜底 | project_memory.md、operational_conventions.md、persona.md、pm.config.yaml |
| **L3 纯运行时** | 无模板的衍生文件 | 纯自愈 | chats/INDEX.md、reflections/*、development_log.md |

### 自愈触发规则

- **Workflow I**：`project_tasks.md` 或 `project_memory.md` 不存在 → 启动 bootstrap
- **Workflow S**：`project_memory.md` 不存在 → 触发 Workflow I
- **Workflow M**：`.pm/user_profile.md` 不存在 → 按模板骨架创建
- **Workflow R**：`.pm/reflections/` 目录不存在 → 创建

### Bootstrap 的幂等性

`pm-bootstrap.py` 对已有文件执行**跳过**而非覆盖。这确保：
- 首次运行：生成干净文件
- 再次运行（如 submodule 更新后）：只补充缺失文件，不覆盖用户修改

## 后果

### 正面

- 新项目启动快：一条命令 `pm bootstrap --from docs/` 完成全部初始化
- 容错性强：误删文件后下次 Workflow 自动恢复（L2/L3 层）
- 模板一致性：L1/L2 文件由统一模板生成，格式规范

### 负面

- Bootstrap 依赖 submodule（`.pm/design/`）就位，无 submodule 时降级为手工路径
- L1 文件如果被删除，自愈无法恢复（必须重新跑 bootstrap）
- 两套创建逻辑（bootstrap 内联生成 vs Workflow 按需生成）需要保持一致

### 待解决

- [ ] L2 层的自愈模板应与 bootstrap 模板同源，避免格式漂移
- [ ] pm.config.yaml 目前由 bootstrap 内联生成，应改为读模板文件 → 已由 `templates/pm/pm.config.yaml` 模板缓解
