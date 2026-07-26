# DiagDoctor 上下文工程设计(现状 + 考量)

> 本文档是上下文工程的**总设计**,反映当前已实现状态(code-verified)+ 设计考量与演进项。
> 定位:**简历项目**。取舍以"面试讲得圆 + 有非 trivial 判断 + 无硬伤"为标准,不做生产级过度工程(见 §8)。
> 记忆系统的检索注入是上下文工程的一环,但记忆系统本身的设计见 `docs/memory_system_design.md`(本文只讲注入侧)。

---

## 1. 目标与定位

给 DiagDoctor 诊断 agent 一套**可控的上下文**:在有限的上下文窗口里,让 LLM 拿到"该拿的"(证据/历史参考/工具结果),挡掉"不该拿的"(噪声/冗余/过期),并在耗尽前优雅收束。

**核心判断**:
- **压缩做在入口,不做在运行时**:证据/工具结果在**入上下文之前**就预处理截断;运行时只做硬收束,不做对话历史摘要压缩。理由见 §5.4(iteration 维度先于 token fire,运行时压缩 ROI 不够)。
- **上下文是增益可控项,不是越塞越好**:RAG 注入、工具结果都有优雅降级,失败/空召回不阻塞诊断。
- **预算多维度,iteration 为主**:token 几乎不触顶,iteration/tool_calls 才是真正 fire 的收束维度(见 §5.2)。

---

## 2. 业界参照与设计原则

| 标杆 | 核心做法 | 本系统借鉴 |
|---|---|---|
| **Claude Code / Cursor** | system prompt + 工具结果截断 + 运行时 compaction | 入口截断(已做);运行时 compaction 评估后不做(§5.4) |
| **LangChain `create_agent`** | middleware 机制,wraps tool_call / before_model / after_agent | 6 个 middleware 分层处理(§3.4/3.5) |
| **Anthropic context engineering** | 区分 compile-time 已知 vs runtime 采集;tool result elision | 入口截断 + 工具去重;符号占位列演进(§7.1) |
| **MemGPT / Letta** | 分层记忆 + 主动管理上下文 | RAG 工具化(agent 带假设查根因,§3.3) |
| **MMR**(Carbonell 1998) | 相关性 - 冗余度 | RAG 注入侧症状层去冗余(见 memory_system_design §7.2) |

**四条设计原则**:
1. **入口优先**:能在入上下文前压的,不留给运行时(更便宜、更可预测)。
2. **分层独立**:system prompt / evidence / tool result / reasoning 各自 token 计数,不混算。
3. **硬收束兜底**:超限 `jump_to:end` + 强制结构化输出,保证总有报告产出。
4. **诚实降级**:任何上下文增强失败都不阻塞诊断,注入空比注入错好。

---

## 3. 分层现状

### 3.1 静态指令层(System Prompt)

- 构造:`_build_system_prompt()`(`engine/agent.py:55`)-> Jinja2 渲染 `prompts/templates/diagnosis_agent.j2`。
- **唯一模板变量** `tools_reference`(`agent.py:43` `load_tools_reference` 读 `tools_reference.md` 全文塞入)。
- 内容:角色定位 + 三步诊断策略 + 工具选择决策表 + 工具清单 + 输出 JSON schema + 预算约束。
- **完全静态**:每次诊断、每个阶段同一份;agent 编译时生成,模块级缓存(`get_diagnosis_agent`)。
- ⚠️ 断层:`ContextBudget.phase` 四阶段(INITIAL/INVESTIGATING/CONVERGING/FINALIZING)算了但没接回 prompt,见 §5.1。

### 3.2 任务输入层(证据注入)

- `format_evidence_for_agent(evidence)`(`evidence/formatter.py`)-> 一条 HumanMessage,**pass 1 一次性全量注入**。
- 内容块:用户报告 / `trigger_trace_ids`(精准查询把手)/ `golden_signals`(最多 30,按 error_log/error_span/slow_span/repeated_query 分类,**不评分**)/ 跨层 `correlations`(最多 10)/ 前端崩溃 spans(最多 5)/ 缺失数据提示。
- 无信号场景有专门引导文案(让 LLM 主动调查 logic/data/config 类)。
- `messages` 用 `add_messages` reducer(`state.py:309`),跨节点/跨 HITL pause-resume 累积保留。

### 3.3 检索增强层(RAG 双通道)

| 通道 | 机制 | 时机 | 向量 | 状态 |
|---|---|---|---|---|
| **P0 静态注入**(症状相似) | `_build_similar_cases_message`(`nodes/diagnosis_agent.py:82`)-> HumanMessage;pass 1 查询并缓存,resume 复用缓存不重查 | agent 跑之前 | 症状向量 | 已实现 |
| **P1-a 工具化**(根因相似) | `search_historical_root_cause` tool(`tools/memory_recall.py`),agent 形成根因假设后主动调 | 运行中,按需 | 独立根因向量 | 已实现 |

注入格式(`case_retriever.py:526` `format_similar_cases`):`## 历史相似诊断参考` + 每个 Case `[id]` + 综合分 + 用户报告 snippet + 根因 + 修复 + 涉及文件,首尾"仅供参考独立判断"。
- **P1-c 冲突检测**:同症状 ≥2 根因时注入"勿锚定 top-1"提示。
- **anti-hallucination**:agent 报告的 `referenced_case_ids` 被 `clamp_referenced_case_ids` clamp 到 ⊆ 实际检索集(只能引用看过的 case)。
- **优雅降级**:失败/空召回/禁用 -> 不注入或短文案,不抛错。
- 检索细节见 `memory_system_design.md`。

### 3.4 工具上下文层(消息通道,三层中间件)

中间件注册顺序(`agent.py:89`):`Lifecycle -> ToolDedup -> LangfuseTracing -> ToolTruncation -> BudgetGuard -> ForcedFinalCall`

| 中间件 | 文件 | 作用 |
|---|---|---|
| **AgentLifecycle** | `middleware/lifecycle.py` | 每轮初始化 budget/计数器/dedup 历史(`abefore_agent`) |
| **ToolDedup**(最外) | `middleware/tool_dedup.py` | `(name, args)` 完全匹配的历史调用短路,返回"[跳过]" |
| **LangfuseTracing** | `middleware/langfuse_tracing.py` | 观测 |
| **ToolTruncation**(最内) | `middleware/tool_truncation.py` | `truncate_tool_result` 按工具类型字符上限截断 |
| **BudgetGuard** | `budget/guard.py` | 多维硬上限 -> `jump_to:end` |
| **ForcedFinalCall** | `middleware/forced_call.py` | 无 JSON 时强制结构化输出 |

工具结果截断策略(`engine/context/truncation.py`):
- 字符上限:`search_observability` 12k / `code_search` 4k / `get_file_content` 8k / `db_query` 3.2k / `inspect_frontend_error` 4k。
- 优先保留含 error/exception/trace/span 等关键词的关键行 + 邻近行;不足再 head 15 + tail 10。
- 对已结构化截断的(`search_observability` 的 `_truncated` 标记)跳过 head/tail--schema-aware 截断优先于行级截断。

### 3.5 预算与收束层

- **ContextBudget**(`engine/context/budget.py`):四维度追踪 token / iteration / tool_calls / time;`phase` 取最严级别(INITIAL->INVESTIGATING->CONVERGING->FINALIZING)。
- **BudgetGuard**(`abefore_model`):`model_call_count > MAX_TOOL_CALLS` 或 `total_used >= MAX_TOKENS_BUDGET` 或 `elapsed >= MAX_TIME_SECONDS` -> `jump_to:end`。
- **ForcedFinalCall**(`aafter_agent`):循环结束若最后一条 AI 消息无 JSON,强制一次 `with_structured_output` 兜底出报告。
- 常量(`engine/budget/constants.py`):`MAX_TOOL_CALLS=12` / `MAX_TOKENS_BUDGET=100_000` / `MAX_TIME_SECONDS=300`。
- ⚠️ 预算测量 split-brain,见 §6.1。

### 3.6 HITL 跨轮延续

- 预算耗尽未收敛(`early_stopped` && `!hitl_resumed`)-> `human_input_node` `interrupt()` 暂停,持久 checkpoint(`data/checkpoints.db`,跨进程可恢复)。
- resume:非空引导 -> `diagnosis_agent` 二次 pass;空 -> END(采纳当前报告)。`hitl_resumed` 门控只允许一次。
- 续查注入(`nodes/diagnosis_agent.py:192`):`【续查模式】` HumanMessage = prior_findings.summary + 操作员引导,fresh budget 跑第二轮。
- `similar_cases_text` 缓存复用(resume 不重查 Qdrant,只重注入)。
- ⚠️ 续查注入偏薄(只 findings.summary),见 §7.2。

### 3.7 可见性过滤

- `_filter_visible_messages`(`nodes/diagnosis_agent.py:69`):剥离 SystemMessage + HumanMessage,只把 AI/Tool 消息流回 CopilotKit 前端(内部上下文不暴露给用户聊天)。

### 3.8 安全与结构化输出

- **PII 脱敏**(`security/sanitizer.py`):识别 [PHONE]/[EMAIL]/[IP] 等占位替换。
- **SQL 守卫**(`security/sql_guard.py`):`db_query` 只允许 SELECT。
- **结构化输出**:JSON schema 在 prompt 里声明 + `ForcedFinalCall` 的 `with_structured_output` 兜底 + `referenced_case_ids` clamp 防幻觉。

---

## 4. 上下文管理策略(三档)

### 4.1 入口压缩(已做 ✅)

入上下文**之前**的预处理,便宜且可预测:

| 策略 | 实现 |
|---|---|
| 证据侧模式折叠 | `evidence/deduplicator.py`:重复日志/trace 折叠成 compact summary(ingest 阶段) |
| 工具结果字符截断 | `truncate_tool_result`:关键行优先 + head/tail |
| 工具结果结构化截断 | `search_observability` 字段级 `_truncated`(schema-aware) |
| RAG 去冗余 | 症状层 MMR(distinct-roots 2.75->2.95) |
| 工具调用去重 | `ToolDedup`:`(name,args)` 完全匹配跳过 |

### 4.2 运行时生命周期管理(基本空 ❌,待补)

对话历史层面**无压缩/无占位/无滑窗**。`compaction.py` + `dynamic_prompt.py` 曾存在,PR #34 删除后无替代。演进项见 §7.1(符号占位)、§7.2(scratchpad)。

### 4.3 收束与兜底(已做 ✅)

BudgetGuard 硬停止 / ForcedFinalCall 强制 JSON / HITL 续查。

---

## 5. 关键辨析与取舍

### 5.1 phase 算了没接(断层)

`ContextBudget.phase`(四阶段)只导出到 `to_dict()`(`budget.py:205`)给 Langfuse/前端看,**没有任何代码消费它去动态调整 prompt 策略**。`diagnosis_agent.j2` 里写了"CONVERGING 减少探索 / FINALIZING 强制收束",但运行时是静态的;BudgetGuard 只在硬超限时 `jump_to:end`,中间无"软收束"。

曾经的 `dynamic_prompt.py` 按 phase 切 prompt,已删。处理:见 §7.5(接回或删死代码)。

### 5.2 "token 到不了 80%"的真相:iteration 先 fire,非"规模"

> ⚠️ `budget.py` docstring 原文"对 15-case 规模,token 几乎到不了 80%"表述不严谨,应改。

**纠正**:一次诊断的 token 消耗与"总共跑多少 case"无关。15 是 experiment 观测样本数(n=15),不是影响单次诊断 token 的变量。

**真相**:单次诊断中,**iteration 维度(`MAX_TOOL_CALLS=12`)先于 token 维度(`MAX_TOKENS_BUDGET=100k`)fire**。粗估(4 chars/token):

| 来源 | token |
|---|---|
| system prompt(tools_reference 全文) | ~2-3k |
| evidence(30 signals + 10 correlations + report) | ~3-4k |
| 12 次循环累积的工具结果(截断后均值 ~2k) | ~24k |
| 12 次 AI reasoning | ~6-12k |
| **第 12 次循环累积峰值** | **~40-55k** |

峰值 ~50k 远低于 100k(---> 80%)。而这又是因为 §4.1 入口截断把单次工具结果压到均值 ~2k,累积才涨得慢。三者协作:

- **入口截断** -> 单次工具结果小 -> 累积 token 增长慢
- **iteration 维度(12 次)** -> 先 fire,卡停 agent
- **token 阈值(100k)** -> 兜底,几乎不触及 80%

**正确表述**(本文档以此为准):
> 单次诊断中,iteration/tool_calls 维度先于 token 维度触发收束(入口截断压小了单次工具结果,累积 token 峰值 ~50k,远低于 100k 阈值);token 阈值是兜底,实际很少触及 80%。

### 5.3 iteration 上限:12 -> 16(标定后,2026-07-26)

曾结论"现在不拉大 MAX_TOOL_CALLS",三条理由:① 无数据(§6.3 未拉分布);② 治标不治本(iteration 是防 flail 护栏,拉大 = 允许 flail 更久,应靠 §7.2 scratchpad 治本);③ 前置雷(iteration 测量 split-brain §6.1,拉大前先 §7.3 单源化)。

**标定后(RAG-off baseline 15-case)**:
- 分布(agent_react,排除 forced):P50=8 / P75=11 / P90=12 / max(收敛)=12;14/15 case 在 5-12 轮自然收敛。
- 12 偏紧:P90 触顶 12。12cap 下 5 个触顶 case,18cap 重测后 4 个(BE-021/DATA-021/FE-020/LOGIC-022)其实 7-12 轮就自然收敛--12 是被非确定性 + forced 兜底偶尔截断。
- FE-021 是 flail:18 轮没收敛(forced + early_stop @18),不是深度不足。加轮不解决,靠 §7.2 scratchpad。
- split-brain(§6.1)已临时修(`guard.py` 算 budget 前先截断,与 agent 收到的裁后口径一致),§7.3 彻底修。

**结论**:`MAX_TOOL_CALLS=16`(P90=12 + 4 buffer,覆盖 14/15;FE-021 触顶 16 靠 forced 出报告 + §7.2 治本)。符合"拉到 16-18(不是 30)"。不拉到 18+:FE-021 已证 flail,再加是给 flail 开绿灯(理由 ②)。配套 §7.1 符号占位(16 cap 下跨轮累积防爆)。

### 5.4 入口压缩 vs 运行时压缩(取舍)

**取舍**:压缩做在入口(预防),不做在运行时(管理)。

- 入口压缩:规则驱动,零额外 LLM 调用,可预测。
- 运行时压缩(LLM 摘要/compaction):成本高,且 §5.2 已证 iteration 先 fire,运行时压缩的触发窗口(token 到 80%)几乎不会到来,ROI 不够。
- 15-case 规模下,运行时压缩的"降本"软收益(每次循环少读点历史)也不足以覆盖其复杂度。

**演化条件**:若未来 case 变难、链条变长(>15 次工具调用),运行时压缩的两种意义(防爆 + 降本)都会变强,届时再上**符号占位**(§7.1,规则驱动,非 LLM 摘要)而非 compaction。

### 5.5 阶段感知动态 prompt:放弃(取舍)

曾按 `ContextBudget.phase` 切换 system prompt 策略(INITIAL 鼓励探索 / CONVERGING 收紧 / FINALIZING 强制收束)。`dynamic_prompt.py` 已删。

**放弃理由**:
- 四阶段提示词不一致,**效果难评估**(没有 ablation 能证明"收束提示"比"硬上限停止"更好)。
- **难展示**:phase 是连续滑变,面试/demo 讲不清"这个 prompt 变在哪、为什么有效"。
- 改成硬阈值收束(80% / iteration 12 -> `jump_to:end`),简单可预测。

### 5.6 sliding window:否决(取舍)

"只保留最近 N 轮消息,老的丢弃"--**否决**,理由:诊断场景早期证据(trigger_trace_ids / golden_signals / 历史案例注入)不可丢,丢弃会破坏证据链。否决掉本身就是取舍故事。

---

## 6. 已知限制与雷点

### 6.1 预算测量 split-brain(雷,待修)

- `MAX_TOOL_CALLS` 实计 `model_call_count`(LLM 调用数),非工具调用数。
- **三处上限不一致**:`constants.py`(`MAX_TOOL_CALLS=12`) / `ContextBudget`(`max_iterations=12`, `max_tool_calls=18`) / `config.py`(`agent_model_context_window=128_000`);`BudgetGuard` 实际只用 constants 那套。
- `ContextBudget` 内的 `max_iterations`/`max_tool_calls`/`phase` 阈值是另一套,且 phase/is_critical 无消费方。
- Langfuse trace 已补 total_tokens(本轮),但未接真实 usage callback(§7.3)。
- 修:见 §7.3(followup-plan #10)。

### 6.2 phase 死代码(雷)

`ContextBudget.phase` / `is_critical` / `is_warning` 算了但无消费方(§5.1)。面试官看 telemetry 有 phase 输出会追问"这字段干嘛的"。处理:见 §7.5。

### 6.3 budget 分布数据(已拉取,2026-07-26)

`_finalize_langfuse_trace` 把 `tool_calls`/`total_tokens`/`early_stopped` 写进 Langfuse trace output_data;本轮经 REST API 拉 session 级 trace 分布(临时脚本,未固化成 `scripts/analyze_budget.py`--§7.4 ⬜)。

**15-case 标定(RAG-off baseline,18cap 重测触顶 case)**:
- agent_react:P50=8 / P75=11 / P90=12 / max=12(14/15),FE-021 触顶 18(flail)。
- early_stop 率:12cap 下 67%(含 CASCADE/PERF token 爆);E(truncation 兜底)+ guard.py split-brain 修复后 token 不再爆,18cap 下仅 FE-021 触顶。
- token:自然收敛 case 5-22k,远低于 100k(§5.2 iteration 先 fire 验证)。

**用途**:§5.3 iteration 上限决策依据(已决策 16);面试讲入口截断/去重效果的数字底气。固化查询脚本见 §7.4。

---

## 7. 演进项

### 7.1 符号占位 / tool result elision(待做)

N 轮之前的 ToolMessage 替换成一行动态符号摘要(工具名+参数+top1 结论),保留"调过什么+关键结论",丢原始大块 JSON。

- **为什么做**:比全量压缩轻(规则替换,零 LLM 成本),比 sliding window 安全(不丢信息只换形式)。
- **配套**:`search_observability` 已有 `analysis.summary` 字段,天然适合做占位摘要来源。
- **待对齐**:① 触发时机(按轮次年龄 N vs 按 usage_ratio 阈值);② 占位字段来源(复用 `analysis.summary` vs 工具统一产 `elision_hint`)。
- **与拉大 iteration 的关系**:不依赖拉大即可做(收益虽小但非零);若未来拉大 iteration(§5.3),符号占位收益放大。

### 7.2 HITL 续查升级:scratchpad(待做)

当前续查只注入 `findings.summary` 一行列表。升级为结构化"已确认事实 / 已排除假设 / 待验证线索"三段。

- **为什么做**:治 flail 本(§5.3 理由 2),让续查上下文结构化,可能让 case 收敛更快,反而降低对 iteration 的需求。

### 7.3 预算单源化 + 接真实 usage(待做,followup-plan #10)

- 统一常量源(去掉 constants/ContextBudget/config 三处不一致)。
- `MAX_TOOL_CALLS` 正名(gate model_call 就叫 model_call,或改回 tool_call)。
- 用 LLM callback 真实 `token_usage` 替 tiktoken 估算。
- **前置**:§5.3 拉大 iteration 决策、§7.4 效果量化都依赖此条。

### 7.4 可观测性闭环:Langfuse trace budget 字段(部分已做)

- ✅ doctor 侧 `_finalize_langfuse_trace` 已补 `total_tokens` + `elapsed_seconds` 到 trace output_data(与 `tool_calls`/`early_stopped` 并列)。
- ⬜ 查询脚本 `scripts/analyze_budget.py`:从 Langfuse Dataset 拉 trace output_data,算分布(P50/P90 tool_calls、early_stop 率、token 峰值)。依赖 Langfuse 在线。
- **为什么做**:上下文工程的效果量化闭环;§5.3 拉大 iteration 的数据依据;面试讲取舍的数字底气。
- 与 §7.3 是同一件事的两面。

### 7.5 phase 接回或删(待定)

`ContextBudget.phase` 二选一:
- **接回**:在 `BudgetGuard.abefore_model` 按 phase 注入软收束提示(CONVERGING 提醒聚焦、FINALIZING 禁止调工具)--补上 §5.1 的断层。
- **删**:若评估软收束 ROI 不够(同 §5.5 放弃理由),删 phase/is_critical/is_warning 死代码,`to_dict` 只留 token/iteration 数值。
- **倾向**:删(与 §5.5 动态 prompt 放弃一致,软收束效果难评估)。但 phase 用于前端 budget 面板可视化的话可保留导出、删消费逻辑。

### 7.6 工具结果过大文本的系统性压缩(待做,着重)

15-case baseline 暴露:CASCADE-020/PERF-020 因 search_observability 返回上千 span(N+1 / retry-storm),tool result 进 context 前 160k-437k token,2 轮 token 爆停。现仅临时止血,需系统性重做。

**已发现失效点 + 临时止血**:
- `search_observability` `Keep ALL traces`(`observability_unified.py:1243-1255`):只 slim 每 span 字段(drop http.*/net.*),不限 span 数量,1694 span 仍巨大。
- `truncate_tool_result` 对 `_truncated` 无条件放行(`truncation.py:77-78`):临时改--超 12k 仍 fall through 裁剪(止血 E)。
- BudgetGuard split-brain(`guard.py:awrap_tool_call`):ctx_budget 在 ToolTruncation 内层,用截断前原始 result 算 token,与 agent 收到的裁后口径不一致,误触发 early_stop(§6.1)。临时改--算 budget 前先 truncate。

**着重做方向(不止止血)**:
1. **重复折叠(结构信号)**:同 name / 同 SQL pattern 的 span 折叠成 `样本 + count + pattern`。N+1 / retry-storm 对症,数量级压缩(1694 -> 1+count)。这是把"识别重复"这步从 LLM 事后能力外化成 tool 侧事前规则。
2. **优先级采样**:error span 全留、slow 按 duration top-N、normal 进 count。预算给异常不给正常。
3. **聚合摘要前置**:result 顶部放 `by_name / by_status / duration` 分布,agent 先看聚合定位。
4. **drill-down / 渐进披露**:agent 看聚合后主动调工具取特定 span 详情(呼应 §3.3 agent 主动查),而非一次全量 dump。
5. **符号占位(§7.1)**:N 轮前的 tool result 替换成一行动态摘要,跨轮累积防爆。
6. **子 agent 压缩**:大量数据的工具(或专门子 agent)在自己的 context 里压缩/筛选,只返结论给主 agent--主 agent 不直接扛全量。trade-off:多一次 LLM 调用,但主 context 干净;适合"原始数据需语义理解才能压缩"(如把 1694 span 交给子 agent 总结成 N+1 结论)。与 §5.4 否决的"对话历史 compaction"不同:子 agent 压缩发生在 tool 侧、主 agent context 不爆,ROI 更高。
7. **跨工具统一**:code_search 大文件、get_file_content 大代码块同策略,不只 search_observability。

**取舍**:规则压缩(重复折叠 / 优先级,稳定可控)做 80%;LLM 摘要(子 agent / 小模型,灵活)做 20%(复杂模式)。子 agent 是"运行时 LLM 压缩"的一种,但限定 tool 侧(主 context 不爆),比对话历史 compaction ROI 高。

**量化**:压缩率、token 节省、对轮次 / 诊断质量影响(依赖 §7.4 闭环)。

**与现有项关系**:§7.1(符号占位)是跨轮那一环;§7.3(单源化)彻底解 BudgetGuard split-brain(现临时修);§7.4 提供量化。本项把工具结果压缩从"字段级 + keep all"升级到"数量级 + 语义感知"。

---

## 8. 不做的(过度工程边界)

| 不做 | 理由 |
|---|---|
| **运行时 LLM 摘要压缩(compaction)** | 成本高;iteration 先 fire 使其触发窗口几乎不来(§5.4);15-case 规模 ROI 不够 |
| **拉大 iteration(当前)** | 无数据支撑 + 治标(§5.3);待 §7.3/§7.4 数据后再评估 |
| **完整 working memory 系统** | 偏重;§7.2 scratchpad 已够治 flail 本 |
| **工具结果小模型实时摘要** | 额外 LLM 调用成本;入口截断 + 符号占位已够 |
| **sliding window** | 诊断早期证据不可丢(§5.6) |
| **阶段感知动态 prompt** | 效果难评估+难展示(§5.5) |

---

## 9. 面试讲法

**主线**:分层上下文工程--入口压缩 -> 运行时占位(演进中)-> 预算收束 -> HITL 延续 -> 效果量化闭环(演进中)。

**三个非 trivial 取舍点**:
1. **压缩做在入口不做在运行时**:讲清 iteration 先 fire 的机制(§5.2 粗算),运行时压缩 ROI 不够。体现"知道上下文要管,但知道什么不管用、为什么"。
2. **多维预算,iteration 为主**:诚实讲 token 到不了 80%,iteration/tool_calls 才是真收束维度;附 split-brain 雷(§6.1)和修法(§7.3),体现"知道自己系统的测量问题"。
3. **动态 prompt 放弃**:讲清四阶段提示词难评估+难展示,改成硬阈值收束(§5.5)。体现"评估过、有取舍"。

**数据底气**(§7.4 完成后):P50/P90 tool_calls 分布、early_stop 率、token 峰值--用数字证明入口截断/去重的效果,而非空谈。

**防雷**:
- 别说"15-case 规模 token 到不了 80%"--case 数与单次诊断 token 无关(§5.2),改说"iteration 先于 token fire"。
- phase 字段要么讲清"用于前端可视化"要么别提,别让面试官追问"这字段干嘛的"(§6.2)。
- `MAX_TOOL_CALLS` 别说成"工具调用上限",它实计 model_call(§6.1)。

---

## 附录:全盘点速查表

| # | 维度 | 现状 | 评价 | 计划 |
|---|---|---|---|---|
| 1 | System prompt 工程 | 静态 j2 + tools_reference | 薄(phase 断层) | §7.5 |
| 2 | 证据注入 | 全量 HumanMessage | 中 | 渐进披露(可选) |
| 3 | RAG 上下文 | 双向量 + MMR + 冲突检测 + clamp | 厚 | 完成 |
| 4 | 工具上下文 | 决策表 + 三层入口截断 + 去重 | 厚 | +§7.1/§7.6 |
| 5 | 上下文生命周期管理 | 入口截断有 / 运行时无 | 薄 | §7.1 |
| 6 | 工作记忆 / scratchpad | 无 | 薄 | §7.2 |
| 7 | 预算 / 窗口管理 | 四维度 + 硬收束 | 中(split-brain) | §7.3 |
| 8 | HITL 跨轮延续 | findings.summary 注入 | 薄 | §7.2 |
| 9 | 安全 / PII | sanitizer + sql_guard + 只读 db | 厚 | 完成 |
| 10 | 结构化输出兜底 | ForcedFinalCall + referenced clamp | 厚 | 完成 |
| 11 | 可观测性闭环 | budget_ticks 同步前端有 / Langfuse trace budget 字段已补、分布已拉取(§6.3,临时脚本) | 中(待固化) | §7.4 |
| 12 | Few-shot 输出示例 | schema 自然语言描述,无完整示例 | 薄(可选) | 可选,ROI 低 |

**整体判断**:不是整体太薄,是薄的恰好在体现深度的位置(运行时管理 / scratchpad / HITL / 可观测性)。补完 §7.1-§7.4 后,薄的只剩 few-shot 和渐进披露(都可选),上下文工程的面即齐。
