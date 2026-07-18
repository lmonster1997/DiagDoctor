# #5 前端 HITL 补齐计划(交接文档)

> 后端 #5(HITL 收窄版)已实现并全绿(ruff + mypy --strict + pytest 328 passed),**未 commit**。
> 本文是前端补齐的执行交接:把后端能力变得在 UI 可见可用。新窗口读此文档即可开干,无需重新调查 CopilotKit 机制。
> 后端实现细节见 `docs/current-status.md` §2.1 + `docs/followup-plan-20260715.md` #5。

## 0. 当前后端已就绪的能力(前端要消费的)

- **3 节点 graph**:`bug_info -> diagnosis_agent -> human_input(#5 HITL 中断点)-> END`。
- **`human_input` 节点** budget 耗尽时 `interrupt({type:"hitl_guidance_request", prompt, prior_findings_count, early_stopped})` 暂停;持久 checkpoint(`data/checkpoints.db`,跨进程可恢复)。
- **resume**:`Command(resume=<guidance>)` 同 `thread_id` 恢复。非空 guidance -> `diagnosis_agent` 知情二次调查(全新 ReAct + 新预算);空 -> END(采纳当前 early_stopped 报告)。一次性门 `hitl_resumed`。
- **state 字段**(外层 DoctorState,CopilotKit `useCoAgent` 会同步):`report`、`early_stopped`、`findings`、`human_guidance`、`hitl_resumed`、`budget`、`budget_ticks`、`messages`(add_messages 累加)。
- **REST 端点**(doctor/backend/src/api/diagnose.py):
  - `POST /api/diagnose` - 跑诊断(budget 耗尽则暂停,返回 early_stopped 报告)。
  - `POST /api/diagnose/resume` `{thread_id, guidance}` - 恢复暂停的诊断(返回最终报告)。
  - `GET /api/diagnose/threads?limit=50` - 列历史线程(`{thread_id, case_id, status: "paused"|"completed"|"empty", early_stopped, hitl_resumed, findings_count, has_report, next}`,paused 置顶)。
- **流式**:`_stream_graph` 在暂停时发 `{"event":"hitl_interrupt", thread_id, prompt, prior_findings_count, early_stopped, next}` 再 `[DONE]`。

## 1. CopilotKit HITL 机制调查结论(关键,勿重复调查)

调查了 `ag_ui_langgraph`(Python,CopilotKit LangGraph 适配)+ `@copilotkit/react-core`(JS)。

### 1.1 在 CopilotChat 里打字 **恢复不了中断**
`ag_ui_langgraph/agent.py:prepare_stream`:
- 检测到 active interrupts 且 **无** `forwarded_props.command.resume` -> **短路**(`agent.py:534-557`):只发 `RunStarted + OnInterrupt + RunFinished` 然后 return,**图根本不跑,用户打的字被丢弃**。
- 只有 `forwarded_props.command.resume is not None` 时(`agent.py:562-582`)才 `stream_input = Command(resume=resume_input)` 真恢复。
- resume 值**只来自 `forwarded_props.command.resume`**,绝不来自聊天文本。

### 1.2 `useCoAgent` **不暴露 interrupt**
`react-core/dist/index.d.mts:445-483`:`useCoAgent` 返回 `{name, nodeName, threadId, running, state, setState, start, stop, run}`。**没有** `interrupts`、`state.next`、interrupt payload。前端只看得到 state values。

### 1.3 正解 = `useInterrupt` hook
`react-core`: `useInterrupt`(`index.d.mts:521-522`,`copilotkit-CmcMFc8o.mjs:4733-4899`;`useLangGraphInterrupt` 是 legacy 别名)。
- 订阅 `onRunFinishedEvent` `outcome==="interrupt"`(及 legacy `on_interrupt`)。
- 暴露 `render({interrupt, interrupts, resolve, cancel})`。`interrupt.value` = 我 `interrupt()` 的 payload(dict)。
- 调 `resolve(payload)` -> `copilotkit.runAgent({agent, forwardedProps:{command:{resume: payload, interruptEvent:...}}})` -> `prepare_stream` 读到 `command.resume` -> `Command(resume=payload)` -> 我的 `human_input` 节点 `interrupt()` 返回 payload。
- **这是显式按钮/卡片动作,不是聊天自由文本。** resolve 后 pass2 经 CopilotKit 正常流式进聊天。

### 1.4 `DiagDoctorAgent.get_state` 是**死代码**
全 `copilotkit` 包 grep `get_state`/`threadExists` 零命中。`src/copilotkit/agent.py:50-75` 的 `get_state`(我 #5 改过先判 `state.next`)**CopilotKit 从不调用**;fresh-vs-resume 决策全在 `prepare_stream` 内(`agent_state.tasks` interrupts + `command.resume`)。我的修复基于错误假设,无害但无用。**清理项**:删掉或注释为 dead code(待定)。

### 1.5 thread_id 正确传递
`ag_ui_langgraph/agent.py:204,476`:`config["configurable"]["thread_id"] = thread_id`。持久 SQLite checkpointer 按 thread_id keyed,跨进程恢复成立。

### 关键文件引用
- `.venv/Lib/site-packages/ag_ui_langgraph/agent.py`(204,206,207-212,225-235,479-485,534-557,562-582)
- `.venv/Lib/site-packages/copilotkit/langgraph_agui_agent.py`(247-251)
- `doctor/backend/src/copilotkit/agent.py`(17-75,dead)
- `doctor/backend/src/api/diagnose.py`(resume 362-400,threads ~402+)
- `doctor/backend/src/engine/nodes/diagnosis_agent.py`(`human_input_node` ~327-376)
- `doctor/frontend/node_modules/@copilotkit/react-core/dist/index.d.mts`(445-483,521-522)

## 2. 前端现状(doctor/frontend)

- `src/main.tsx`:`<CopilotKit runtimeUrl="/api/copilotkit" useSingleEndpoint={false} renderToolCalls={...}>`。
- `src/pages/DiagnosePage.tsx`:`useCoAgent<AgentState>({name:"default"})` + `<CopilotChat>`。右侧面板 3 tab(进度/证据链/初步分析)。**无 useInterrupt、无 history、无 resume 接线**。
- `src/features/diagnosis/parseAgentState.ts`:从 state/chat messages 解析 report/findings/evidence(支持外层 graph 直接字段)。
- `src/features/diagnosis/CaseInputChat.tsx` / `FollowUpCard`(`DiagnosePage.tsx:205-241` 定义但**未渲染**)。
- `src/api/client.ts`:`apiFetch<T>(path, options)`(BASE_URL `localhost:8001`)。**未接 /threads、/resume**。
- `src/api/types.ts`:DiagnosisReport/Finding/BudgetState 等类型。
- 包管理:**pnpm**(`doctor/frontend/package.json`)。

## 3. 要做的三件事

### F1. `useInterrupt` 引导卡(核心,杀手级 demo UX)
图暂停时在聊天里渲染引导卡,`resolve` 恢复。
- 卡片内容:"预算耗尽,诊断未收敛。已收集 {prior_findings_count} 条发现:<findings 摘要,从 state.findings 取>。请补充一句人工引导(如可疑方向/已知线索):" + 文本框 +「续查」按钮 +「采纳当前」按钮。
- `resolve(guidanceText)` -> 恢复走 pass2(流式进聊天)。「采纳当前」= `resolve("")` -> END(保留 early_stopped 报告)。
- `interrupt.value` = `{type, prompt, prior_findings_count, early_stopped}`;findings 摘要从 `useCoAgent` 的 `state.findings` 取(更丰富)。
- **先验**:写最小 `useInterrupt` 验证这个 CopilotKit 版本(1.62.x)的准确 API(`render` 签名、resolve 调用、卡片渲染位置--是否需在 `CopilotChat` 内或独立),确认 `resolve` 能接上 `human_input`。这是 F1 唯一不确定点,先 spike。
- 接线点:`DiagnosePage.tsx`(或 `main.tsx` 的 provider 内)加 `useInterrupt`。

### F2. 完成信号 + 续问(便宜)
- report 就绪时渲染已定义的 `FollowUpCard`(`DiagnosePage.tsx:205-241` 现成)+ 明确"诊断完成,可继续提问"文案/状态灯。
- 区分三种终态:收敛(early_stopped=false,蓝灯+续问卡)、early_stopped 完成一次性 END(橙灯+续问卡)、暂停等引导(useInterrupt 卡,F1)。

### F3. 历史诊断列表(中)
- 新组件消费 `GET /api/diagnose/threads`(经 `api/client.ts` 加 `listThreads()`)。
- 列表项:`thread_id` 截断、`status`(paused 高亮置顶)、`early_stopped`、`findings_count`、时间。
- 点 **paused** -> 载入该 thread 到聊天恢复(需切 CopilotKit thread_id;spike `useCoAgent` 的 threadId 切换或 `start({threadId})`)。点 **completed** -> 查看 report(只读)。
- 入口:header 加"历史"nav,或诊断页右侧加 tab。v1 可只做"列表 + 复制 thread_id + 跳恢复",click-to-resume 作为 v2。

## 4. demo 触发(真实模型)
真实模型多数 <12 calls 收敛不暂停。要可靠演示 F1,临时把 `doctor/backend/src/engine/budget/constants.py` `MAX_TOOL_CALLS` 调到 ~4(改完 uvicorn `--reload` 自动重载),demo 完恢复 12。
- 注意:cap=4 时 pass2 也可能再耗尽 -> 一次性 END(照样展示 pause->resume->END 机制,只是不一定"引导后收敛")。确定性"引导后收敛"证明在 `tests/graph/test_hitl.py`。

## 5. 验证
- 后端:`uv run ruff check .` + `uv run mypy --strict src` + `uv run pytest`(基线 328 passed/1 skipped)。前端改动不应让后端退化。
- 前端:`cd doctor/frontend && pnpm build`(或 `pnpm lint`)。手测:启动后端 `uv run uvicorn src.main:app --port 8001 --reload` + 前端 `pnpm dev`,cap 调 4,聊天输 bug -> 见引导卡 -> 输引导续查 -> 收敛。

## 6. 已定决策(后端,勿改)
- `messages` 用 `add_messages`(跨 pass 保历史)。续查=知情二次调查(非真 ReAct 续传)。一次性 HITL(`hitl_resumed` 门)。HITL 活在共享 graph(benchmark 中性)。未做:真 ReAct 续传、多轮 HITL、循环中插消息(off-framework)。

## 7. 清理项
- `DiagDoctorAgent.get_state`(死代码):删或注释(见 §1.4)。`execute` 同文件亦未被调,一并审视。
- `.claude/plan.md`(若残留):scratch,删。

## 8. 未 commit 的后端改动(8 文件)
`docs/current-status.md`、`docs/followup-plan-20260715.md`、`doctor/backend/src/api/diagnose.py`、`doctor/backend/src/copilotkit/agent.py`、`doctor/backend/src/engine/nodes/diagnosis_agent.py`、`doctor/backend/src/engine/state.py`、`doctor/backend/tests/graph/test_checkpointer_reducer.py`、`doctor/backend/tests/graph/test_hitl.py`(新)。建议新窗口第一步先 commit 后端(在 `feat/checkpioter` 分支),给前端工作干净基线。
