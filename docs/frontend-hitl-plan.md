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
`react-core`: `useInterrupt`(`copilotkit-DnLpJ2Cl.d.mts:2473`;`useLangGraphInterrupt` `index.d.mts:522` 是薄封装,内部调 `useInterrupt`,`index.mjs:1552`)。
- Config:`{ render, handler?, enabled?, agentId?, renderInChat? }`。`renderInChat` 默认 `true`/undefined = **卡片自动渲染进 `<CopilotChat>`**(hook 返回 `void`);`false` = 返回元素手动放置。**F1 用默认值,无需手动布局。**
- `render({ interrupt, interrupts, resolve, cancel, event, result })`。调 `resolve(payload)` / `cancel()`。
- 调 `resolve(payload)` -> `copilotkit.runAgent({agent, forwardedProps:{command:{resume: payload, interruptEvent:...}}})` -> `prepare_stream` 读到 `command.resume` -> `Command(resume=payload)` -> 我的 `human_input` 节点 `interrupt()` 返回 payload。
- **这是显式按钮/卡片动作,不是聊天自由文本。** resolve 后 pass2 经 CopilotKit 正常流式进聊天。

### 1.3.1 Spike 实测结论(2026-07-18,源码级验证,纠正 §1.3 两处细节)
在 1.62.2 实际包里钉死了准确 API 与触发路径:
- **触发的是 legacy `on_interrupt` 路径,不是 standard `outcome="interrupt"`**。`ag_ui_langgraph/agent.py:539-551` 发 `CustomEvent(name=on_interrupt)` + **无 `outcome`** 的普通 `RunFinishedEvent`。`outcome` 字段在整个 `.venv/site-packages` + `@ag-ui` + `@copilotkit` node_modules **0 命中** -> `useInterrupt` 的 `onRunFinishedEvent outcome==="interrupt"`(umd:3232)不可达,`localStandard` 恒 null;`INTERRUPT_EVENT_NAME="on_interrupt"`(umd:3175)匹配 -> `pending.kind==="legacy"`。
- **render 里 `interrupt` 是 `null`、`interrupts` 是 `[]`**(umd:3378-3379,legacy kind)。payload 在 **`event.value`**(=`interrupt()` 的 dict,经 `toV1Event` JSON 解析回)。⚠️ §1.3 说的"`interrupt.value` = payload"只对 standard 路径成立,本后端不触发。**F1 必须读 `event.value`(或 `interrupt?.value ?? event?.value` 兼容写法),勿读 `interrupt.value`。**
- **resolve 必须传纯字符串**。后端 `agent.py:563-581`:string resume 先试 `json.loads`,失败保留原串。`resolve("续查文本")` -> `guidance` 非空 -> pass2;`resolve("")` -> `has_resume_input` 真(`"" is not None`),保留 `""` -> `guidance_str=""` -> END 采纳当前。传对象/数组 -> `str(guidance)` 变 repr 垃圾,两分支全坏。
- 接线已确认:`mount.py:21-26` 把 `get_copilotkit_graph()`(3 节点含 `human_input`,`diagnosis_agent.py:419-441`)传给 `DiagDoctorAgent`,CopilotChat 跑的就是外层 graph,interrupt 能流到 `useInterrupt`。`parseAgentState` 那条"绕过外层 graph/直调内层 create_agent"注释是 #5 之前旧状,已过时(外层 state 字段现在经 `useCoAgent` 直接同步,`parseAgentState` 走 direct-fields 分支)。
- 挂载点:`useInterrupt` 须在 `<CopilotKit>` provider + chat 配置上下文内(`useCopilotKit()` + `useAgent()`)。`DiagnosePage.tsx` 已有 `useCoAgent`,直接在此挂 `useInterrupt`,`renderInChat` 默认即自动进现有 `<CopilotChat>`。

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
- `messages` 用 `add_messages`(跨 pass 保历史)。续查=知情二次调查(非真 ReAct 续传)。一次性 HITL(`hitl_resumed` 门)。HITL 活在共享 graph(REST/CopilotKit 中性)。未做:真 ReAct 续传、多轮 HITL、循环中插消息(off-framework)。

## 7. 清理项
- `DiagDoctorAgent.get_state`(死代码):删或注释(见 §1.4)。`execute` 同文件亦未被调,一并审视。
- `.claude/plan.md`(若残留):scratch,删。

## 8. 未 commit 的后端改动(8 文件)
`docs/current-status.md`、`docs/followup-plan-20260715.md`、`doctor/backend/src/api/diagnose.py`、`doctor/backend/src/copilotkit/agent.py`、`doctor/backend/src/engine/nodes/diagnosis_agent.py`、`doctor/backend/src/engine/state.py`、`doctor/backend/tests/graph/test_checkpointer_reducer.py`、`doctor/backend/tests/graph/test_hitl.py`(新)。建议新窗口第一步先 commit 后端(在 `feat/checkpioter` 分支),给前端工作干净基线。

## 9. 实现状态(2026-07-18,F1+F2+F3 已完成并验证通过)
Spike 结论见 §1.3.1。**根因 + 修法见 §9.3**(CopilotKit 后端丢 forwardedProps,自定义端点修)。
- **F1 引导卡**:`doctor/frontend/src/features/diagnosis/GuidanceCard.tsx`(新)+ `DiagnosePage.tsx` 挂 `useLangGraphInterrupt<HitlPayload>`(主入口,legacy on_interrupt 路径,`event.value`=payload)。续查/采纳当前 -> CopilotKit `resolve(value)` -> `copilotkit.runAgent({forwardedProps:{command:{resume}}})` -> `POST /api/copilotkit/agent/default/run`。卡片自动渲染进现有 `<CopilotChat>`(`renderInChat` 默认)。**resume 经 §9.3 自定义端点把 `forwardedProps.command.resume` 送进 `RunAgentInput.forwarded_props` -> ag_ui_langgraph `Command(resume)` -> human_input 续查/pass2 流式进聊天。**
- **F2 完成信号 + 续问卡**:状态灯 5 怞(amber-pulse 等待引导 / blue 收敛 / amber-static early_stopped 完成 / cyan 分析中 / grey 就绪,永不绿色);`hitlPending` 标志(handler 置 true,resolve 包装 + `running` effect 清除)区分暂停 vs 完成;现成 `FollowUpCard` 接到右面板 footer(`report && !hitlPending` 时显示)。
- **F3 历史列表**:`api/client.ts` 加 `listThreads()` + `DiagnosisThread` 类型;`HistoryPanel.tsx`(新,react-query 消费 `GET /api/diagnose/threads`);右面板第 4 tab「历史」。paused 高亮置顶 + 复制 thread_id +「切换并恢复」(`useCopilotContext().setThreadId`)。**CORS 修复**:`client.ts` BASE_URL 改相对路径(走 vite `/api` 代理同源),与 CopilotKit `runtimeUrl="/api/copilotkit"` 一致;原绝对 `http://localhost:8001` 跨源且 vite 跑 5174(不在 `cors_origins=[3000,5173]` 白名单)-> OPTIONS 400。**`/threads` 子图 checkpoint 修复**:`list_diagnosis_threads` 原把 `saver.alist` 返回的子图 checkpoint config(`checkpoint_ns="diagnosis_agent"`,因 `_diagnosis_agent_node` 内调 `create_agent` 子图)直接传 `aget_state` -> langgraph 解析子图失败 `ValueError: Subgraph diagnosis_agent not found` -> 500。改为 `aget_state({"configurable":{"thread_id":tid}})` 取根 namespace 最新状态 + try/except 跳过无法解析的。

### 9.1 F3 v2 待办(click-to-resume)
v1「切换并恢复」仅 `setThreadId` 切线程;引导卡在下一次交互时浮现(§1.1 短路,会吃掉那条消息)。v2:切换后自动 `useCoAgent().start()` 立即浮现引导卡(免交互、免吃消息)。需运行时验证 start() 在线程切换后的竞态(threadId 是否已提交)。

### 9.2 验证(2026-07-18 全绿)
- 后端:`uv run ruff check .` ✓;`uv run mypy --strict src` ✓(73 文件);`uv run pytest` ✓ **329 passed**(基线 328 + 新增,无回归;1 个预存 Starlette httpx 弃用 warning)。
- 前端:`pnpm exec tsc -b` 本次文件零错误;`pnpm lint` 本次文件零 warning(唯一余 warning 是预存 `ToolCallCard.tsx` `Settings` 未用);`pnpm exec vite build` ✓。
- **预存 2 个前端配置错误**(非本次引入,`feat/HITL` 已有):`tsconfig.app.json` `baseUrl` 弃用(TS6,需 `ignoreDeprecations:"6.0"`)+ `vite.config.ts` `historyApiFallback` 不在 vite8 `ServerOptions`。使 `pnpm build`(`tsc -b && vite build`)卡在 tsc;`vite build`/`lint` 均过。建议单独修。
- 真机手测通过(用户确认):cap=4 聊天输 bug -> 引导卡 -> 续查 -> `copilotkit_hitl_human_input_resumed` + pass2 流式进聊天 + 卡片消失 + 报告更新。

### 9.3 F1 resume 根因 + 修法(CopilotKit 后端丢 forwardedProps)
原设计(§1.3)用 `useLangGraphInterrupt` 的 `resolve(payload)` -> `copilotkit.runAgent({forwardedProps:{command:{resume}}})` -> 后端 `Command(resume)`。**运行时实测不工作**:点续查后 `POST /api/copilotkit/agent/default/run` 200,但后端无 `copilotkit_hitl_human_input_resumed`,引导卡卡死、聊天无输出。

**根因(逐步定位)**:
1. 前端 Network 体**有** `forwardedProps: {command:{resume:"..."}}`(确认前端发了)。
2. 后端 `ag_ui_langgraph` `run(self, input)` 断点处 `input.forwarded_props` **空**,`input.resume` None,`input.thread_id` 对(确认后端没收到)。
3. `ag_ui` `RunAgentInput` 有 `to_camel` alias(`forwardedProps`->`forwarded_props`)+ `populate_by_name`,反序列化本应工作 -> 排除 alias 问题。
4. 查 CopilotKit 后端:`copilotkit/integrations/fastapi.py` 的 `/agent/{name}` handler 从 body **只取** `threadId/state/messages/actions/nodeName`,**丢 `forwardedProps`**;`handle_execute_agent` -> `sdk.execute_agent` -> `agent.execute` 整条链都不传 forwarded_props。所以 `RunAgentInput.forwarded_props` 空 -> `prepare_stream` 不 resume。**这是 CopilotKit 后端 SDK 的能力缺口(不转发 forwardedProps),非前端问题。**

**修法**:`doctor/backend/src/copilotkit/run_endpoint.py` 新增自定义 `POST /api/copilotkit/agent/default/run` 端点,从 body 取 `forwardedProps` 塞进 `RunAgentInput.forwarded_props`,直接调 `agent.run`(复用 `execute` 的 `EventEncoder` 流式)。在 `mount_copilotkit` 里 `add_fastapi_endpoint` **之前**注册(`app.add_api_route`),优先匹配,绕过 CopilotKit 丢 forwardedProps 的 handler。正常诊断(forwarded_props 空)和 resume(带 command.resume)都走它,行为一致。pass2 经 ag_ui_langgraph 正常流式进聊天(保留了 §1.3 的流式 UX,无代价)。

**废弃方案**:曾尝试改走 REST `/api/diagnose/resume`(可靠但 pass2 不流式进聊天,用户拒绝该代价),已回退;`client.ts` 的 `resumeDiagnosis` 已删。最终用上述自定义端点保留流式。

### 9.4 待办(非阻塞)
- F3 v2 click-to-resume(§9.1)。
- 预存前端配置债(§9.2 tsconfig/vite)。
- 清理项 §7:`DiagDoctorAgent.get_state`(死代码,§1.4)+ `execute`(自定义端点接管 `/agent/default/run` 后,execute 对 run 也成死代码;但 `get_state` 仍被 `/agent/{name}/state` 用)。一并审视。
