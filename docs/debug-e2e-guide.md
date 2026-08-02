# DiagDoctor 端到端调试指引

按一次诊断的时间顺序排列。每条：**断点位置** → **看什么变量**。

## 数据流

```
前端 CopilotChat 输入
  → CopilotKit SDK POST /api/copilotkit/agent/default/run
  → vite proxy → :8001
  → [HTTP 中间件] CorsPreflight / InfoCompat
  → default_agent_run 构造 RunAgentInput
  → DiagDoctorAgent.run → ag_ui_langgraph → graph.astream
  → bug_info 节点 (提取 bug 信息 + prefetch + normalize)
  → diagnosis_agent 节点 (set_run_context → 内部 create_agent.ainvoke)
       └ 7 中间件 ReAct 循环
  → route_after_diagnosis (early_stopped? → human_input interrupt : END)
  → SSE ag_ui 事件流回前端
  → useCoAgent 同步 state + parseAgentState 解析
  → 卡片渲染 (ToolCallCard / ReportPanel / GuidanceCard / BudgetPanel)
```

启动：用 VSCode 的 `🐍 Doctor API (debug)` 配置（:8001，`justMyCode:false` 能进第三方库断点）。
前端：`cd doctor/frontend && pnpm dev`（:5174）。

---

## 1. 前端发送

### 普通发送（用户输入 bug 描述点发送）
- **[DiagnosePage.tsx:199](../doctor/frontend/src/pages/DiagnosePage.tsx#L199)** `<CopilotChat>` 组件挂载点 — 发送按钮在这个库组件内部，**用户代码拦不到发送动作**。
- 断点方式：
  - DevTools → **Network** → 看 `POST /api/copilotkit/agent/default/run` 的 **Request**（`messages` / `threadId` / `forwardedProps`）和 **Response**（SSE 流）。
  - DevTools → **Sources → XHR Breakpoints** → 加 `agent/default/run`，可在 fetch 发出前断下看调用栈。

### HITL 续查发送（诊断暂停后点续查/采纳）
- **[GuidanceCard.tsx:91](../doctor/frontend/src/features/diagnosis/GuidanceCard.tsx#L91)**「采纳当前」按钮 / **[GuidanceCard.tsx:99](../doctor/frontend/src/features/diagnosis/GuidanceCard.tsx#L99)**「续查」按钮 → `handleResolve(value)`。
- **[DiagnosePage.tsx:106](../doctor/frontend/src/pages/DiagnosePage.tsx#L106)** `resolve(v)` → 看 `v`（非空字符串=续查；空串=采纳当前）。`resolve` 触发 CopilotKit 发起带 `command.resume` 的请求。

---

## 2. 后端 HTTP 入口

- **[run_endpoint.py:41](../doctor/backend/src/copilotkit/run_endpoint.py#L41)** `body = await request.json()` → 看 `body`（前端到底发了什么：`threadId` / `messages` / `forwardedProps` / `nodeName`）。
- **[run_endpoint.py:53](../doctor/backend/src/copilotkit/run_endpoint.py#L53)** `run_input = RunAgentInput(...)` → 看 `forwarded_props`（resume 时应带 `command.resume`）。

### agent bridge / 是否续跑
- **[copilotkit/agent.py:60](../doctor/backend/src/copilotkit/agent.py#L60)** `state = await self.graph.aget_state(...)` → 看 `state.next`（非空=paused 要 resume）、`state.values.report`（有=已完成要 fresh）。
- 第三方库 `ag_ui_langgraph/agent.py` 的 `prepare_stream` → 看 `forwarded_props.command.resume` 是否到达。**HITL resume 已知脆弱点**，需 `justMyCode:false` 才进得去。

---

## 3. bug_info 节点

- **[bug_info.py:223](../doctor/backend/src/engine/nodes/bug_info.py#L223)** `tid = ...thread_id` → 看 `tid`（== `case_id` == checkpoint key，三者必须一致）。
- **[bug_info.py:264](../doctor/backend/src/engine/nodes/bug_info.py#L264)** `bug_info = await _extract_bug_info(...)` → 看 `bug_info`（`bug_description` / `trigger_time` / `trace_ids`）。
- **[bug_info.py:356](../doctor/backend/src/engine/nodes/bug_info.py#L356)** `normalized = ingest(raw_dict)` → 看 `normalized`（`golden_signals` / `correlations`）。

---

## 4. diagnosis_agent 节点

- **[diagnosis_agent.py:213](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L213)** `evidence_text = format_evidence_for_agent(evidence)` → 看 `evidence_text`（喂给 LLM 的证据）。
- **[diagnosis_agent.py:251](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L251)** `similar_msg, rag_updates = ...` → 看 `similar_msg`（RAG 召回的历史 case）。
- **[diagnosis_agent.py:311](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L311)** `set_run_context(run_ctx)` → 看 `run_ctx`（`case_id` / `evidence_text` / `system_prompt_text`）。
- **[diagnosis_agent.py:320](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L320)** `result = await agent.ainvoke(...)` → 步进进去就是中间件循环；返回后看 `result["messages"]`。
- **[diagnosis_agent.py:353](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L353)** `report, findings, budget_state, early_stopped = _finalize_report_for_dict_state(...)` → 看 `report` / `findings` / `early_stopped`（最终输出）。

---

## 5. 7 中间件（ReAct 每轮都过一遍）

注册顺序（[agent.py:90](../doctor/backend/src/engine/agent.py#L90)）：
`AgentLifecycle → ToolDedup → LangfuseTracing → ToolTruncation → ContextElision → BudgetGuard → ForcedFinalCall`

- **[lifecycle.py:23](../doctor/backend/src/engine/middleware/lifecycle.py#L23)** `abefore_agent` → 看 `ctx` 初始化后 `ctx_budget` / `call_history` 是否清空。
- **[tool_dedup.py:44](../doctor/backend/src/engine/middleware/tool_dedup.py#L44)** `call_key = (...)` → 看 `call_key`、`ctx.call_history`（命中=跳过）、`ctx.elided_tool_call_ids`（命中=放行重取）。
- **[context_elision.py:70](../doctor/backend/src/engine/middleware/context_elision.py#L70)** `for rank, idx in ...` → 看 `replacements`（被 ageing 成占位的 ToolMessage）、`keep_recent`=3。
- **[budget/guard.py:28](../doctor/backend/src/engine/budget/guard.py#L28)** `ctx.model_call_count += 1` → 看 `model_call_count`（>12 停）、`ctx.ctx_budget.total_used`（>100k 停）、`elapsed_seconds`（>300s 停）。**预算门控核心断点**。
- **[budget/guard.py:68](../doctor/backend/src/engine/budget/guard.py#L68)** `usage = getattr(msg, "usage_metadata", None)` → 看 `usage.input_tokens`（模型真实 context 大小）。
- **[forced_call.py:44](../doctor/backend/src/engine/middleware/forced_call.py#L44)** `if _last_ai_has_json(messages)` → 看是否走兜底 JSON 调用（`ctx.forced_call_triggered`）。
- **[langfuse_tracing.py:62](../doctor/backend/src/engine/middleware/langfuse_tracing.py#L62)** `awrap_tool_call` → 看工具 span 记录（`tool_name` / `elapsed_ms`）。

---

## 6. HITL 暂停与恢复

- **[diagnosis_agent.py:517](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L517)** `guidance = interrupt(...)` → 看 `prompt`（图卡在这里等 resume）。
- **[diagnosis_agent.py:532](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L532)** `return {"human_guidance":..., "hitl_resumed":True}` → resume 后看 `guidance_str`（非空=续查，空=采纳当前）。
- 回到 **[run_endpoint.py:48](../doctor/backend/src/copilotkit/run_endpoint.py#L48)** `forwarded_props = body.get("forwardedProps")` → 看 `forwarded_props.command.resume` 这次有没有到后端。

---

## 7. 前端接收 / 卡片渲染

- **[DiagnosePage.tsx:39](../doctor/frontend/src/pages/DiagnosePage.tsx#L39)** `useCoAgent({name:"default"})` → 看 `state`（`report` / `findings` / `evidence` / `budget_ticks` / `case_id` / `similar_cases_text`）、`running`。
- **[DiagnosePage.tsx:82](../doctor/frontend/src/pages/DiagnosePage.tsx#L82)** `parseAgentState(state, chatMessages)` → 看 `report` / `findings` / `evidence`（卡片数据来源）。
- **[parseAgentState.ts:349](../doctor/frontend/src/features/diagnosis/parseAgentState.ts#L349)** `if (state?.report || ...)` → 看 `state` 有没有 `report` 字段（没有就走 `chatMessages` 里抠 JSON）。
- 卡片本体：
  - **[ToolCallCard.tsx:122](../doctor/frontend/src/features/diagnosis/ToolCallCard.tsx#L122)** 工具调用插片（看 `props.name` / `args` / `result` / `status`）。
  - **[ReportPanel.tsx:58](../doctor/frontend/src/features/diagnosis/ReportPanel.tsx#L58)** 报告卡（看 `report`）。
  - **[GuidanceCard.tsx:38](../doctor/frontend/src/features/diagnosis/GuidanceCard.tsx#L38)** HITL 引导卡（看 `payload` / `findings`）。

---

## 调试手段速查

| 手段 | 适用 | 怎么用 |
|---|---|---|
| VSCode 断点 | 后端节点/中间件 | `🐍 Doctor API (debug)`，`justMyCode:false` 可进第三方库 |
| Network 面板 | 前后端边界 | 看 `agent/default/run` 的 Request body + SSE Response |
| XHR Breakpoints | 前端发送前 | DevTools → Sources → XHR Breakpoints → `agent/default/run` |
| structlog 日志 | 全链路追迹 | `data/logs/doctor.log`，按 `case_id` grep（== thread_id） |
| Langfuse | LLM/tool 过程 | :3002，看 trace 下的 LLM call + tool span |
| React DevTools | 前端 state | 看 DiagnosePage 的 `state` / `running` |

## 已知坑

- **HITL resume 的 `command.resume` 可能不到后端**：GuidanceCard/DiagnosePage 注释标注"调查中"。重点断 `ag_ui_langgraph prepare_stream` 的 `forwarded_props.command.resume`，以及 [run_endpoint.py:48](../doctor/backend/src/copilotkit/run_endpoint.py#L48)。
- **PG 5432 抢占**：Windows 上 `postgresql-x64-16` 服务自启会抢赢 docker，demo-app 实际用本机 PG。doctor 的 `demo_db_ro_url` 必须 `127.0.0.1`（不是 localhost），且 docker PG 要停。
- **ProactorEventLoop + psycopg async 崩**：doctor 跑 ProactorEventLoop，`AsyncConnection` 会崩 `InterfaceError`。`db_query` 已改 sync + `asyncio.to_thread`。CLI 测绿 ≠ 运行时真生效，必须在 Proactor 下实测。
- **预算 cap**：`MAX_MODEL_CALLS=12` / `MAX_TOKENS=100k` / `MAX_TIME=300s` / `RECURSION_LIMIT=200`（[budget/constants.py](../doctor/backend/src/engine/budget/constants.py)）。

## 最小走查

先在 [run_endpoint.py:41](../doctor/backend/src/copilotkit/run_endpoint.py#L41) 和 [diagnosis_agent.py:353](../doctor/backend/src/engine/nodes/diagnosis_agent.py#L353) 各打一个断点，跑一次，确认"请求进来 → 报告产出"两头通；再按需往中间细化。中间件循环出问题就盯 [budget/guard.py:28](../doctor/backend/src/engine/budget/guard.py#L28)。
