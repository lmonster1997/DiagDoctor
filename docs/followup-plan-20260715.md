# DiagDoctor 架构对标与后续计划(2026-07-16)

> 视角:**【当前实现 vs 业界优秀 bug 诊断 agent 系统】的架构对标**,非"声明 vs 代码对齐 / 除雷"。
> 现状盘点见 `current-status.md`(权威 reality map);本文向前做架构判断与 ROI 排序。
> 已 commit 的止血项(测试 CI、forced call 守卫、上下文死代码清理、Langfuse 凭据、截断默认开)**不再复盘**。
> 长期记忆系统以 `long_term_memory_design.md` 为权威设计,本文仅作架构衔接。

---

## 1. 对标基线

用以对标的业界 bug 诊断 / RCA agent 谱系,及 DiagDoctor 的对标位置:

| 谱系 | 代表 | 核心设计 | DiagDoctor 对应 |
|---|---|---|---|
| AIOps 图谱化 RCA | MicroRCA(Wu et al., CNSM 2020) | 服务依赖图 + 异常关联图 + 社区检测定位根因服务,**纯确定性** | evidence 管线 cross-layer `trace_id` 关联(简化版) |
| 自动软件工程 agent | SWE-agent(Yang et al., NeurIPS 2024)+ SWE-bench | Agent-Computer Interface(ACI):工具输出简洁可信 / 反馈回路 / guardrail;ReAct over terminal;失败测试 grading | `create_agent`+middleware ReAct + 工具契约(部分)+ benchmark LLM-judge |
| observability-driven 诊断 | Datadog Watchdog RCA / Amazon DevOps Guru | 建在 traces/logs/metrics 之上:异常检测 + 关联异常/部署/配置变更 -> probable causes | `search_observability` auto 模式(agent 主动查 OTel/Loki/Tempo) |
| LangGraph 诊断范式 | LangGraph 官方 multi-agent / 诊断示例 | typed state+reducer / 持久 checkpointer / `interrupt()` HITL / streaming / tool node | 2-node graph+middleware(部分:dict 非 typed、`MemorySaver` 非持久、无 interrupt) |
| 学术 bug 定位 | SBFL(Ochiai/Tarantula 谱法)/ IR-based(BugLocator、BLUiR 按 bug report 文本相似度排文件) | 测试谱 / 信息检索定位嫌疑文件 | `code_search`(ripgrep 精确匹配,**刻意不做**语义向量的 IR 近似) |

**共性设计(对比有据,非主观)**:

1. **确定性证据/关联外置**--MicroRCA 图算法、Datadog 异常检测、SBFL 谱法都把"便宜的确定性关联"放在 LLM/启发式大脑之外。DiagDoctor evidence 管线 + `search_observability` 检测器同此。
2. **observability 即数据源**--Watchdog/DevOps Guru 的 RCA 建在自己的可观测栈上。DiagDoctor 让 agent 主动查同一套 OTel/Loki/Tempo = 把这一模式做成了 agent 工具(差异化)。
3. **根因 = 关联异常社区的"中心"**--MicroRCA 社区检测、Datadog probable-cause 图。DiagDoctor 跨层 `trace_id` 关联是其简化实例。
4. **agent loop guardrail + ACI**--SWE-agent ACI(输出简洁可信、反馈回路、guardrail)+ 预算/迭代上限。DiagDoctor BudgetGuard+ToolDedup+ForcedFinalCall+截断同此。
5. **eval-driven dev + 单一可信 grading harness**--SWE-bench。DiagDoctor benchmark+LLM-judge 同此(但分裂,见 §2.4)。
6. **结构化输出即契约**--SWE-agent diff、Datadog insight。DiagDoctor forced_call 同此。

> **一句话定位**:DiagDoctor = 把 "observability-driven RCA(Datadog/DevOpsGuru)" + "SWE-agent ACI 式工具循环" 用 LangGraph 编排成单 agent,配 bug-factory 量产可评 case。对标位置清晰;**差距在「记忆闭环 / 评测单一可信 / 可观测自省 / HITL」四处的完成度,而非方向错误**。

---

## 2. 架构正确性(逐层)

### 2.1 编排与状态

- **方向对**:`create_agent`+middleware 优于手写 while 循环(Iteration 3 迁移);ContextVar 每调用状态(`DiagnosisRunContext`,run_context.py)是"被复用的无状态 middleware 拿到 per-run 数据"的正确解法--middleware 实例在 graph compile 时绑定,`self` 上存 mutable state 会跨诊断泄漏;BudgetGuard 用原生 `@hook_config(can_jump_to=["end"])` + `jump_to` 硬停,而非手写 flag。三点都是 LangGraph 1.0 习语,非玩具。
- **硬伤·reducer 失效**:`StateGraph(dict)` 而 `DoctorState` 声明了 `Annotated[list, add]` / `add_messages` 等 reducer(state.py:264-282)。**reducer 根本没跑**,state 走 dict 覆盖语义:`findings`/`budget_ticks`/`hypotheses` 的 `add` 累加是空的,节点 return `{"findings":[...]}` 是覆盖不是追加。typed schema 声明了但没接线 = 反模式。对标 LangGraph = `StateGraph(DoctorState)` 让 reducer 真跑。
- **硬伤·预算三配置 + 两套计算**:constants(`MAX_TOOL_CALLS=12`/`MAX_TOKENS=100k`/`MAX_TIME=300s`)vs `ContextBudget`(`max_iterations=12`/`max_tool_calls=18`/`max_time=180s`)vs config 三处不一致;且 `MAX_TOOL_CALLS` 实际 gate 的是 `model_call_count`(LLM 调用数,guard.py:31)而非 tool_call 数--命名误导。更糟是**两套预算对象**:活的 `ContextBudget`(tiktoken cl100k_base **估算**,在 middleware gate via `jump_to`)vs 展示给前端/判 `early_stopped` 的 `BudgetState`(tracker.py,事后从 messages 数 tool_calls + 估算 token)。两者测量方式不同、数值不同--**agent 用的预算和给用户看的预算不是同一个数**。
- **死件·phase 空转**:`ContextPhase`(INITIAL/INVESTIGATING/CONVERGING/FINALIZING,budget.py:36-48)算了但**从不注入 prompt**(`diagnosis_agent.j2` 是静态 3 步)。phase 机制是空转脚手架。(注:四阶段动态策略注入实测无收益已弃用--正确决策;但 `ContextPhase` 残留应删或接回。)
- **缺口·不可恢复**:`MemorySaver` 内存态(重启丢);无 `interrupt()` HITL。对标 LangGraph 诊断范式 = 持久 checkpointer + interrupt 是长调查诊断 agent 标配(可暂停/续查/人工引导)。

**取舍讲法**:dict + MemorySaver 是为 CopilotKit 兼容的权宜,代价是 typed reducer 失效 + 不可恢复。下一步要么 `StateGraph(DoctorState)` + 持久 checkpointer 做实,要么删 reducer 声明并文档化 dict schema--不能两头悬空。

### 2.2 工具契约

- **方向对·code_search**:`code_search.py:349-383` 刻意**不做 vector fallback**,无匹配时返回结构化"下一步建议"--正是 SWE-agent ACI 原则"别给 agent 假安全网":语义向量对代码标识符不可靠,宁可引导换工具也不返回低质命中。`file_role` 分类(code_search.py:61-106)给 agent 语义角色。工具设计真理解。
- **方向对·search_observability auto**:observability-as-data-source 的 agent 化(Loki->trace_id->Tempo->span 树->N+1/bottleneck/cascade/timeout 检测->因果链),对标 Datadog/DevOpsGuru 但做成 agent 主动调的工具。
- **硬伤·工具失信**:`source_map_resolve` 是 stub(原样返回 input + "passthrough"),却被 `inspect_frontend_error` 默认调用、工具描述宣传"Source map 还原"。ACI 第一原则是工具输出必须可信--一个原样返回的工具会让 agent 在 minified 位置上推理。
- **硬伤·5 种返回/错误形态不兼容**:5 个活跃工具返回/错误形态各异,模型得逐工具学。对标 ACI = 统一 envelope。
- **双源漂移**:`tools_reference.md` 手维护、agent 构建时注入 system prompt(agent.py:43-52),与 tool schema 双源(k 默认 10 vs 文档 5 已漂移)。对标 = tool schema 即契约,单一来源。
- **衰败**:3 个 observability 模块(observability_tools / unified / trace_query)+ deprecated 工具仍导出;`frontend_tools` 死但被 import 私有 helper(耦合);evidence helper(`_get_service_name`/`_get_trace_id`/`derive_tier`)重复 4 份。

### 2.3 记忆

- **写入侧真**:`case_store.maybe_index_diagnosis` 把诊断写入 Qdrant(用户点赞触发),payload 结构化。
- **最大架构缺口·记忆 write-only**:检索侧 `case_retriever.search_historical_cases` 零调用点,`triage.j2` 的 `{{ similar_cases }}` 无消费方。对标 case-based reasoning / RAG-augmented diagnosis:**记忆是 LOOP**(写 -> 检索 -> 注入 -> 评测 -> 写),半截记忆只是 write-only log。**系统不会越用越准**。
- **评测侧缺口**:`retrieval_gold` 字段在 recipe 里定义但无 RetrievalEvaluator 消费--连"检索有没有用"都测不了。

### 2.4 评测

- **方向对**:LLM-judge + structured output + cache + fallback;`score_process_quality` 用 evidence_coverage 而非调用数惩罚(不奖励偷懒少调工具但漏根因的 agent);`score_category_accuracy` 主动防 gold 泄漏;bug-factory recipe schema 丰富(多标签 + cross_layer + tier + retrieval_gold + expected_observation)+ traceparent 注入 + ui_reachable 门禁 = eval-data 工程真功夫。
- **硬伤·三套评分打架**:benchmark 4 维 / langfuse 7 维 / `run_case.py` 5 维,维度权重 prompt 各不同。对标 SWE-bench = **单一可信 grading harness**。没有单一可信分数,就做不了 eval-driven dev--所有 ablation 都是空口。
- **硬伤·judge 脆弱**:LLM judge 单次无自一致性 + 静默失败(`except: return 0.0`,与"诊断全错"不可区分)+ judge 模型隔离仅 local 路径(langfuse 路径回落到与 doctor 同模型)。judge 质量 = 评测质量的天花板。
- **硬伤·ground truth 不可信**:`expected_observation`(log_patterns/trace_attributes)从不校验--**bug 没真触发也能产出合法 case**。+ 不可复现(trigger_trace_id 随机、user_report LLM 无 seed)。对标 eval integrity = 激活门禁 + 可复现是非谈判项。
- **缺口·无 ablation**:截断 / RAG / budget 各机制到底有没有用,无法量化。

### 2.5 可观测

- **方向对且深**:Langfuse 逐 generation 抓**真实 token_usage**(非估算)+ 工具 span(latency/iteration)+ structured-output span + dedup skip 事件;7 维 scorer + process_quality;全程优雅降级。demo-app OTel 管线正确(traceparent -> FastAPI -> SQLAlchemy commenter -> Collector -> Tempo/Loki -> Grafana)。**agent 用 observability 诊断**是核心 agent-eng 故事。
- **硬伤·只观测"病人"不观测"医生"**:`TokenAccountant` 完全死代码(cost_usd 恒 0)、`bind_log_context` 从不调用(trace_id 不进日志)、无 per-phase 成本归因、无 Langfuse↔Tempo 跨系统 trace_id 链接(不可导航)。对标 Datadog/DevOpsGuru = 它们观测自己的诊断过程。**建了 observability-driven diagnosis,却没建 observability OF the diagnosis**。
- **硬伤·handler 可变单例**:并发诊断 trace 串台。对标 = per-invocation handler 工厂。

---

## 3. 面试亮点(真拉开档次,每条带"为什么是信号 + 怎么讲取舍")

1. **forced_call 结构化输出**
   - 信号:真懂 structured output 的工程坑,不是调个 API。
   - 讲取舍:"默认 `json_schema` method 被 DeepSeek 400(`response_format unavailable`),改 `function_calling`;`include_raw=True` 留 raw 排查回归;解析出的 Pydantic 在 `on_llm_end` 之后才物化,callback 看不到,我加了显式 Langfuse span 记录 parsed 对象。再加 `_last_ai_has_json` 守卫--健康 run(最后一条 AI msg 已含 JSON)跳过这次额外全历史调用,只有 mode1(cap + 空 content)/mode2(叙事无 JSON)才触发兜底。"

2. **search_observability auto 模式(observability-as-data-source)**
   - 信号:把 Datadog/DevOpsGuru 的 RCA 模式做成了 agent 主动调的工具,项目差异化核心。
   - 讲取舍:"N+1/bottleneck/cascade/timeout 检测是便宜确定性活,我留在工具里(MicroRCA 式:确定性关联外置),不塞进 LLM 脑子。stale-window 自动纠正防 agent 抄 prompt 里硬编码示例日期查到过期空结果。"

3. **evidence 管线(确定性 tiered)**
   - 信号:懂"什么留 LLM 外"的取舍。
   - 讲取舍:"denoise/dedup/signal_extract/correlate 纯 Python 确定性,agent 从 golden signal 起步而非裸日志--便宜、可复现、可单测。跨层 `trace_id` 关联是 MicroRCA 社区检测的简化版。"

4. **手写循环 -> create_agent+middleware 迁移(Iteration 3)**
   - 信号:成熟度(识别手写循环维护成本),比"一直手写"强。
   - 讲取舍:"迁移代价是丢了手写循环的注入点;我用 ContextVar per-invocation state 让被复用的无状态 middleware 拿到 per-run 数据,不在 `self` 上存(middleware 实例 compile 时绑定,会跨诊断泄漏)。"

5. **budget guard + jump_to + ContextVar(带诚实讲 split-brain)**
   - 信号:harness 工程真理解 + 敢讲未修的债。
   - 讲取舍:"多维预算(iteration/token/time)+ 原生 `jump_to='end'` 硬停。诚实讲:我有个 split-brain 要修--活的 `ContextBudget`(tiktoken 估算,gate 循环)和展示的 `BudgetState`(事后数 tool_calls)是两套测量,数值不同;`MAX_TOOL_CALLS` gate 的实为 `model_call_count`。下一步单源化 + 接真实 usage。"

6. **Langfuse 7 维 scorer + process_quality**
   - 信号:scorer 设计有取舍演进。
   - 讲取舍:"process_quality 用 evidence_coverage 不用调用数--不奖励偷懒 agent。`score_category_accuracy` 主动剥 gold 泄漏词。scorer 注释记了为何弃朴素 dedup / budget ratio。"

7. **bug-factory recipe schema + traceparent + ui_reachable + collect_diff**
   - 信号:eval-data 工程真功夫,跨层 bug 可评是差异化。
   - 讲取舍:"跨层 bug 难评,我注 W3C traceparent 让后端采纳触发 trace、`ui_reachable` 门禁强制经真实浏览器、`collect_diff` 对无信号 bug 用 `access_control_anomaly`/`silent_data_loss` 显式捕获。"

8. **doctor 前端 EvidenceChainGraph / ToolCallCard / BudgetPanel**(支撑项)
   - 信号:真展示 agent 推理(信号->关联->finding->report 侦探板 + 编号工具步 + 预算脉冲),非通用图表。

---

## 4. 问题与优化点(按简历 ROI 排序)

排序口径:**故事深度优先**--优先能讲成 agent 工程深度故事的(RAG 闭环 / 单一可信评测+ablation / judge 加固 / HITL+checkpoint),"深化已有" > "铺新摊子"。每项标:解决什么架构/设计问题 / agent 岗信号强度 / 成本。

### T1 核心 agent 工程故事(最高 ROI)

| # | 方向 | 解决的架构/设计问题 | 信号 | 成本 |
|---|---|---|---|---|
| 1 | **RAG 检索侧--闭合记忆 LOOP** | 记忆 write-only 无学习闭环;`case_retriever.search_historical_cases` 零调用点。实现检索 + top-k 相似 case 注入 diagnosis(结构化 few-shot "相似已解 bug 的诊断路径",非裸塞 `similar_cases`) | ⭐⭐⭐⭐⭐ | 1.5d |
| 2 | **单一可信评测 + ablation harness** | 三套评分打架(benchmark 4 / langfuse 7 / run_case 5)无法 eval-driven dev。统一为 canonical harness + 同 case 集 × 配置开关(截断 / RAG / budget)对比,产"配置 X 让 overall ±Y、token ±Z"表 | ⭐⭐⭐⭐⭐ | 2.5d |
| 3 | **LLM judge 加固** | 单次 + 静默失败(`return 0.0` 不可区分)+ judge 不隔离。失败返 None;root_cause 维 k=3 自一致性;judge ≠ agent 强制隔离;5 case 人工一致性集报 judge-human agreement | ⭐⭐⭐⭐⭐ | 1.5d |
| 4 | **评测完整性:激活门禁 + 可复现** | `expected_observation` 从不校验(bug 没触发也合法 case)+ 不可复现。取证后校验 log_patterns(缺失标 invalid 不入库);case metadata 记 `generator_model`/`temperature`/`generation_seed` | ⭐⭐⭐⭐ | 1d |
| 5 | ✅ done **HITL interrupt + 持久 checkpointer(收窄版)** | 无 HITL / 不可恢复;LangGraph 诊断标配。scope 收窄:**中断点 + 一条人工引导消息 + 从 checkpoint 恢复续查**,非完整协同编辑。**已实现(2026-07-18)**:`human_input` 节点 `interrupt()` + `Command(resume=guidance)` 从持久 checkpoint 恢复;budget 耗尽 -> 暂停 -> 知情二次调查(全新 ReAct + 新预算);一次性门 `hitl_resumed`(二次耗尽直奔 END);`messages` 切 `add_messages` 跨 pass 保聊天历史;REST `POST /api/diagnose/resume` + `GET /api/diagnose/threads` + 流式 `hitl_interrupt` 事件;CopilotKit `get_state` 修暂停态 resume;`tests/graph/test_hitl.py` 6 case headless 全绿(聊天 UI 待浏览器 smoke-test)。benchmark 中性(不调 /resume 返回同一份 early_stopped 报告) | ⭐⭐⭐⭐⭐ | 2d |

> HITL 诚实边界(讲清,免被当过度工程):15-case headless benchmark **无法量化** HITL 价值,价值在交互 demo + 面试叙事;走 CopilotKit 交互路径,与 benchmark headless 路径并存不冲突--与 RAG"边界判断"同理。

### T2 深化已有(中高 ROI)

| # | 方向 | 解决的架构/设计问题 | 信号 | 成本 |
|---|---|---|---|---|
| 6 | **观测"医生"**:`TokenAccountant` 接线 + `bind_log_context` 进 FastAPI middleware + per-phase 成本归因 + Langfuse↔Tempo 跨系统 trace_id 链接 | 只观测病人不观测医生;cost_usd 恒 0;trace_id 不进日志;Langfuse↔Tempo 不可导航。`trigger_trace_ids[0]` 设 doctor OTel span 属性 `diag.bug_trace_id` + Langfuse trace metadata | ⭐⭐⭐⭐ | 1.5d |
| 7 | ✅ done **`StateGraph(DoctorState)` 真 reducer + 持久 checkpointer** | reducer 声明了不跑 = 反模式;dict 覆盖;`MemorySaver` 重启丢。`StateGraph(DoctorState)` 让 reducer 真跑 + 换 SqliteSaver/PostgresSaver。**#5 HITL 的地基**。已实现(commit `960d63b`) | ⭐⭐⭐⭐ | 0.5d |
| 8 | **`RetrievalEvaluator`(消费 `retrieval_gold`)** | 死字段;code_search 头牌能力未量化。从 doctor 工具调用参数 / report `evidence_refs` 算 hit-rate 对 `retrieval_gold.code_chunks` | ⭐⭐⭐⭐ | 1d |

### T3 工具/预算契约(中 ROI,便宜)

| # | 方向 | 解决的架构/设计问题 | 信号 | 成本 |
|---|---|---|---|---|
| 9 | **`source_map_resolve` 实现或移除** | 工具失信(ACI 第一原则)。stub 原样返回 input 却被默认调用 + 宣传。真做 source map 还原或删工具 + 改描述 | ⭐⭐⭐ | 0.5d |
| 10 | **预算单源化 + 接真实 usage** | 三配置 + 两套计算 + tiktoken 估算。统一常量源;`MAX_TOOL_CALLS` 正名(gate model_call 就叫 model_call);用 LLM callback 真实 `token_usage` 替 tiktoken 估算 | ⭐⭐⭐ | 0.5d |
| 11 | **Langfuse handler 工厂** | 可变单例并发串台。改 per-invocation 工厂 | ⭐⭐⭐ | 0.3d |
| 12 | **统一工具返回/错误 envelope** | 5 种不兼容形态。统一 `{status,data,error?,hint?}` + `ToolError` 约定 | ⭐⭐⭐ | 1d |

### T4 支撑(低 ROI)

| # | 方向 | 解决的架构/设计问题 | 信号 | 成本 |
|---|---|---|---|---|
| 13 | **demo-app IDOR 修补** | `tasks.py`/`comments.py` get/update/delete 无 ownership 校验 = 预存漏洞污染受控注入评测(logic_020 正靠移除 owner 过滤注入 IDOR,baseline 不干净) | ⭐⭐⭐ | 0.5d |
| 14 | **安全守卫接线** | `sanitize_path`/`sanitize_for_llm`/`safe_subprocess_args` 死代码;`file_reader` 手写重复沙箱未复用 `sanitize_path` | ⭐⭐ | 1d |

### 推荐执行顺序(故事深度优先 + 依赖)

1. ~~**#7**~~(✅ done,commit `960d63b`)+ ~~**#5**~~(✅ done,2026-07-18):编排正确性地基 + 杀手级 demo(budget 耗尽 -> 暂停 -> 人工补一句 -> 恢复续查)已落地。**下一步:#1**(RAG 检索侧,闭合记忆 LOOP)。
2. **#1**(RAG 检索侧,1.5d)-> **#8**(RetrievalEvaluator,1d):闭合记忆 LOOP + 量化检索,故事自洽("越用越准"且可测)。
3. **#2**(单一可信评测 + ablation,2.5d)-> **#3**(judge 加固,1.5d)-> **#4**(激活门禁+可复现,1d):eval-driven dev 支点--#2 让 #3/#4/#8 都可量化验证。
4. **#6**(观测医生,1.5d)+ **#9/#10/#11**(便宜除雷,1.3d)穿插。
5. T4 按需。

### 明确不做(过度工程 / 已决策)

| 方向 | 原因 |
|---|---|
| 多 agent 回归(Triage/Specialist/Critic) | V3 有意收敛为单 agent,尊重决策;把单 agent 做深而非铺摊子 |
| Bug case 扩到 30+ / 变异引擎 | 15 case 跨 8 类对简历够,已决策不做 |
| K8s + Helm 部署 | 超 side project 定位;讲设计思路即可 |
| 全功能 prompt 管理平台(版本/ablation/eval 绑定全套) | 过度工程;registry 加 `REQUIRED_VARS` + Langfuse 记版本 hash 即够 |
| 四阶段动态策略注入 | 实测无收益,已弃用(正确决策);`ContextPhase` 残留待清 |
| 多租户/生产部署 | 超出定位 |
