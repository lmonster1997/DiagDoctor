# CopilotKit Harness 迭代优化日志

> 从 `main` 分支拉出，2026-07-11 起始
> 目的：把 CopilotKit 诊断路径的质量从"能用"提升到与 REST API 路径持平
> 方法：case 驱动增量（继承 `harness-iteration-log.md` 方法论）
>
> **背景**：REST API 路径（`ingest → diagnosis_agent → reporter`）在 Iteration 2 达到
> overall mean=0.909 / 0 disasters。CopilotKit 路径（`bug_info → diagnosis_agent`）
> 当前质量明显更低——工具调用 22+ 次、频繁 budget 耗尽、文件定位错误。

---

## Baseline 现状

### 观测到的失败模式（BE-020 单 case 多次复现）

| Trace ID | 路径 | 工具数 | early_stopped | affected_file | 根因关键词 |
|----------|------|--------|--------------|---------------|-----------|
| 4d7ac448 | REST API | 11 | false | `backend/app/api/comments.py` ✅ | IntegrityError, 外键约束 ✅ |
| 4346b064 | CopilotKit | 14 | true | `app/services/comment_service.py` ❌ | 缺失 ❌ |
| 88adbc45 | CopilotKit | 22 | true | `app/services/comment_service.py` ❌ | 缺失 ❌ |
| 1900b23f | CopilotKit | 14 | true | `app/services/task_service.py` ❌ | 缺失 ❌ |

### 已修复的基础设施问题

| 问题 | 修复 | 状态 |
|------|------|------|
| Langfuse tracing 未初始化 | `execute()` 中初始化 `DiagnosisRunContext` | ✅ |
| Graph 缺少 checkpointer | `compile(checkpointer=MemorySaver())` | ✅ |
| 消息不持久化（前端消失） | 节点返回 `messages` | ✅ |
| `run_ingest` 工具冗余 | 从工具集移除 + 提示词更新 | ✅ |
| 截断阈值过小（8000→24000） | 截断优先保留 error 日志 | ✅ |
| 时区 `+00:00` 被误剥离 | 保留 tzinfo | ✅ |
| `_setup_trigger_time` 缺失 | bug_info + diagnosis_agent 节点添加 | ✅ |

### 当前残留的退化原因

| 优先级 | 问题 | 影响 |
|--------|------|------|
| P0 | 证据文本只有信号摘要，不含实际日志内容 | Agent 只知道"unhandled_exception"但不知道是 IntegrityError，必须手动查日志 → 多轮 search_observability |
| P0 | 系统提示词缺效率指引 | Agent 反复 search↔code 乒乓，22 次调用才被迫停 |
| P1 | `_finalize_report_for_dict_state` 是低质量副本 | BudgetState 恒为空，tool_calls 计数错误 |

---

## 第 1 章 · 迭代日志

### Iteration CK-0: Baseline 确认

- **日期**：2026-07-11
- **基础设施修复已完成**（见上表 7 项）
- **当前 CopilotKit 表现**：BE-020 上 tool call 14-22 次，early_stopped=true，affected_file 始终错误
- **REST API 对照**：BE-020 overall 0.97，11 次工具调用，自然 stop
- **差距归因**：
  1. 证据信号摘要太粗粒度（缺异常类型关键词）
  2. Agent 无效率约束，反复搜索
  3. 代码重复导致的记录错误（不影响诊断但影响可观测性）

---

### Iteration CK-1: 证据富化 + 效率提示词 + BudgetState 修复

- **日期**：2026-07-11
- **维度**：证据工程 + Prompt 工程 + 可观测性
- **针对的 case**：BE-020（22 tool calls, budget 耗尽, file 错误）

**观测到的失败模式**（从 1900b23f vs 4d7ac448 对比）：
- 证据文本只显示 `unhandled_exception` 信号摘要，不含异常类型
- Agent 被迫手动调 `search_observability` 获取完整日志 → 反复横跳 → 22+ 工具调用
- REST API 路径 4d7ac448：11 次工具，自然 stop，`IntegrityError` 关键词命中

**加的 3 个机制**：

1. **证据富化 — 错误日志摘录**（`bug_info.py` + `copilotkit_graph.py`）
   - 代码：`bug_info.py:_attach_error_log_excerpts()` + `_diagnosis_agent_node` 追加
   - 逻辑：prefetch 后扫描 error 日志，取前 300 字符 ×5 条存入 `NormalizedEvidence.metadata["error_log_excerpts"]`
   - 证据文本末尾追加 `【关键错误日志摘录】` 章节
   - 为什么不是过拟合：只加原始日志内容，不改信号摘要，Agent 自己判断

2. **系统提示词效率纪律**（`diagnosis_agent.j2`）
   - 删除「第一步必须先调 search_observability」（误导）
   - 新增「每工具最多 3 次」「不要在 search↔code 之间横跳」「看到异常类型立刻 code_search」
   - 新增「第 1 步新增：检查日志摘录中是否有具体异常类型」

3. **BudgetState 修复**（`copilotkit_graph.py:_finalize_report_for_dict_state`）
   - 从 `BudgetState()` 空对象 → `update_budget(BudgetState(), agent_result)` 真实统计
   - 新增 `is_budget_exceeded(budget_state)` 双重检查

- **单元测试**：待补
- **预期效果**：
  - Agent 看到 `IntegrityError` 关键词 → 立刻 `code_search("IntegrityError")` → 命中 `comments.py`
  - 工具调用从 22 → ≤12
  - `tool_calls` 计数正确
- **结论**：❌ **CK-1 prompt 改动导致 REST API 路径退化**，见 CK-2

---

### Iteration CK-2: 回退 CK-1 效率提示词（修复 REST API 退化）

- **日期**：2026-07-11
- **维度**：Prompt 工程
- **针对的 case**：BE-020 REST API 路径退化（0.97→0.76）

**观测到的退化**（CK-1 prompt 上线后，REST API 路径 BE-020 验证）：

| 指标 | `12e4f14` 框架迁移 | CK-1 prompt 后 | Δ |
|------|--------------------|-----------------|----|
| 工具调用 | 11（自然 stop） | 22（budget 耗尽） | **+11** |
| overall | 0.97 | 0.76 | **-0.21** |
| affected_file | `api/comments.py` ✅ | `comment_service.py` ❌ | 错误 |

CopilotKit 路径（e5cbca2b）：14 tool calls，early_stopped，forced_final_json_call 触发了但 `parse_diagnosis_report` 返回 None → "Agent 未输出有效 JSON"。

**根因分析**：

CK-1 在 `diagnosis_agent.j2` 中加了 3 类改动：

1. **去掉「第一步必须先调 search_observability」→ 改为「先思考，不急着调工具」**
   - 后果：Agent 失去了"先拿完整数据"的强制性引导。BE-020 的证据只有 3 个粗糙信号（`unhandled_exception` + 2 slow span），没有异常类型。Agent 被迫在 search↔code 之间反复试探。

2. **新增「效率纪律（必须遵守）」：每工具≤3 次、禁止横跳、总上限 12 次**
   - 后果：这些手铐反而让 Agent 在证据不足时不敢充分搜索。REST API 路径 22 次调用中大量是 search↔code ping-pong——"禁止横跳"的规则没生效，但"每工具≤3次"可能迫使 Agent 在不该切换时切换。

3. **新增「优先利用已提供的证据」**
   - 后果：BE-020 的证据不含异常类型信息，Agent 被暗示"少调工具"，但证据又不足以诊断 → 陷入死循环。

**关键认知**：CK-1 prompt 效率纪律是**用一个路径（CopilotKit）的问题去约束另一个路径（REST API）**。CopilotKit 的 22+ 工具调用问题是证据不足（无异常类型）导致的，应该通过**证据富化**（`_attach_error_log_excerpts`）解决，而不是通过 prompt 给所有路径加手铐。

**回退操作**：`diagnosis_agent.j2` 回退到 CK-1 之前的版本（≈`12e4f14` 时期）：
- 恢复「第 1 步：获取完整数据 → 先用 search_observability」
- 移除「效率纪律」全部 5 条
- 移除「优先利用已提供的证据」警告
- 移除「检查错误日志摘录」步骤（该功能仅 CopilotKit 路径使用，不应在共享 prompt 中）
- 保留「第 2 步线索→工具对照表」和「第 3 步 JSON 输出」的通用指导

**保留的 CK-1 机制**（仅 CopilotKit 路径，不影响 REST API）：
- ✅ `_attach_error_log_excerpts`（证据富化）
- ✅ `_finalize_report_for_dict_state` BudgetState 修复

- **结论**：待重新跑 BE-020 双路径验证
