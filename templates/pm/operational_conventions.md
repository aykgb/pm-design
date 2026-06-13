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
