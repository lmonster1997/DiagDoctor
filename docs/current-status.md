# DiagDoctor 项目实现现状(2026-07-15)

> 准确描述当前代码**实际实现与运行状态**,作 README 定稿前的权威替身(README 待项目完成后生成准确版)。
> 后续计划见 `followup-plan-20260715.md`;长期记忆系统以 `long_term_memory_design.md` 为权威设计。
>
> 状态标记:**✅ 已实现且在跑** / **⚠️ 已实现但未接线或默认关闭** / **🔲 未实现**。⚠️/🔲 项后跟 followup-plan 编号。

---

## 1. 系统概览

DiagDoctor = LLM 诊断 agent:给定出错 Web 应用(demo-app/TaskFlow)+ 错误现象 + 日志/trace,编排工具定位根因并给修复建议,由 Langfuse 评测。

三个子系统:
- **demo-app**:被诊断目标(TaskFlow,FastAPI + React)
- **bug-factory**:AI 辅助生成 + 注入 bug,量产可复现评测 case
- **doctor**:诊断 agent 主体(LangGraph)

**doctor 主图(实际)**:3 节点 `bug_info -> diagnosis_agent -> human_input -> END`(含 #5 HITL 中断点);reporter 已并入 diagnosis_agent 的 forced final call。diagnosis agent 用 LangChain `create_agent()` + 6 middleware 管线(**非手写 while 循环**;Iteration 3 从手写循环迁移而来,借 middleware 找回注入点)。状态为 typed `DoctorState`(TypedDict,声明的 `add` reducer 真跑);checkpointer 为持久 `_LazyAsyncSqliteSaver`(`data/checkpoints.db`,#7)。

middleware 顺序:`AgentLifecycle -> ToolDedup -> LangfuseTracing -> ToolTruncation -> BudgetGuard -> ForcedFinalCall`。

---

## 2. doctor 后端

### 2.1 engine(编排)
- ✅ 3 节点图(含 #5 HITL 中断点)+ `create_agent` + 6 middleware,顺序有文档
- ✅ **forced_call 结构化输出**:`with_structured_output(method="function_calling", include_raw=True)` + Langfuse span 记录解析对象(method 选 function_calling 是为避 DeepSeek 对 json_schema 的 400)
- ✅ BudgetGuard 多维(iteration/token/time)+ 原生 `jump_to="end"` 硬停
- ✅ ContextVar 每调用状态(`DiagnosisRunContext`),middleware 实例无状态
- ✅ ToolDedup(字节级同 `(name,args)` 去重)+ 优雅 Langfuse 降级
- ✅ forced final call **条件触发**(`_last_ai_has_json` 守卫已接进 `ForcedFinalCallMiddleware.abefore_model`;健康 run 跳过额外结构化输出调用,预算耗尽仍触发兜底)-> A3 done
- ✅ 上下文工程:死代码 `maybe_compact_context`/`build_dynamic_system_prompt`(及 `test_context_engine.py`)已删;`tool_result_truncation_enabled` **默认 True**(ToolTruncation 中间件激活,长结果入 context 前截断保留关键行)-> A4 done
- ✅ `DoctorState` 为 typed graph schema(TypedDict,reducer 真跑:`findings`/`hypotheses`/`budget_ticks`/`total_cost` 用 `add` 累加;`messages` 用 `add_messages` 累加——跨 HITL pause/resume 保聊天历史);checkpointer 换持久 `_LazyAsyncSqliteSaver`(`data/checkpoints.db`,重启不丢)-> #7 done
- ✅ **#5 HITL 收窄版**:`human_input` 节点 `interrupt()` + `Command(resume=guidance)` 从持久 checkpoint 恢复;budget 耗尽 -> 暂停 -> 人工补一句 -> 知情二次调查续查(全新 ReAct + 新预算);一次性门 `hitl_resumed`(二次耗尽直奔 END 不循环)。REST `POST /api/diagnose/resume` + `GET /api/diagnose/threads` + 流式 `hitl_interrupt` 事件;CopilotKit `get_state` 修暂停态 resume。`tests/graph/test_hitl.py` 6 case 全绿(headless 可验;CopilotKit 聊天 UI 待浏览器 smoke-test)-> #5 done
- ✅ graph 测试套件已修(`src.graph.*`->`src.engine.*`;reorg 后无法修复的旧集成测试以 `_` 前缀禁用,CI 全绿)-> A2 done
- ⚠️ `MAX_TOOL_CALLS` 实计 model_call 数,且 constants/ContextBudget/config 三处上限不一致;commit 称"flailing 检测"实未实现(仅硬上限 + 语法去重)-> B8

### 2.2 tools(6 个活跃:search_observability / code_search / db_query / inspect_frontend_error / get_file_content / search_historical_root_cause)
- ✅ **code_search**:ripgrep 精确匹配,无匹配时返回结构化"下一步建议"(非假向量结果)
- ✅ **search_observability** auto 模式:Loki -> 提 trace_id -> Tempo -> 跨层 span 树 -> N+1/bottleneck/error span/cascade/timeout 检测 -> 因果链 -> insights;含 stale-window 自动纠正(防 agent 用 prompt 里的硬编码示例日期)
- ✅ **db_query**:app 层 sql_guard(sqlparse token walk + raw-regex 兜底 + 多语句拒绝 + first-keyword 检查)
- ✅ file_reader、inspect_frontend_error(浏览器错误分类 + 组件名抽取)
- ✅ **search_historical_root_cause**(P1-a,§6.4):根因向量检索历史相似 bug,agent 形成根因假设后主动调,独立 `rag_root_cause_tool_enabled` 开关;优雅降级(空库/无匹配/失败均返字符串不抛)
- ⚠️ **source_map_resolve 是 stub**(原样返回 input + "passthrough"),却默认被 inspect_frontend_error 调用、工具描述宣传"Source map 还原" -> A8
- ⚠️ 无统一工具返回/错误契约(5 种不兼容错误形态)-> B6
- ⚠️ 三个 observability 模块(observability_tools / observability_unified / trace_query)+ deprecated 工具仍导出;`frontend_tools` 死但被 import 私有 helper(耦合)
- ⚠️ tools_reference.md 手维护,已与 tool schema 漂移(k 默认 10 vs 文档 5)

### 2.3 evidence 管线(确定性纯 Python,在 `evidence/`)
- ✅ `tier_aware -> denoise -> dedup -> signal_extract -> correlate` 流水线;golden signal(error_log / error_span / slow_span / browser_error)+ 跨层 trace_id 关联
- ⚠️ helper 重复(`_get_service_name`/`_get_trace_id`/`derive_tier` 在 correlator/signal_extractor/tier_aware/trace_query 各一份)
- ⚠️ `repeated_query`(N+1)信号 docstring 列了但 ingest 不 emit,仅 agent 主动调 search_observability 时出

### 2.4 prompts
- ✅ `diagnosis_agent.j2`(静态 3 步策略 + 工具选择表)+ Jinja2 registry
- ⚠️ `triage.j2` 死模板(triage 节点 V3 已删);registry 无版本/必需变量校验
- ⚠️ `tools_reference.md` 在 agent 构建时注入 system prompt,与 tool schema 双源漂移

### 2.5 observability
- ✅ **Langfuse 集成是真 LLM 可观测**:逐 generation 抓真实 `token_usage`(非估算)+ 工具 span(带 latency/iteration)+ structured-output span + dedup skip 事件;7 维 scorer + `score_process_quality`(evidence_coverage);全程 `contextlib.suppress` 优雅降级
- ✅ **demo-app OTel 管线正确**:前端 FetchInstrumentation 注 W3C traceparent -> 后端 FastAPIInstrumentor 提取 -> SQLAlchemy commenter -> Collector -> Tempo/Loki -> Grafana(tracesToLogsV2 关联)
- ✅ **agent 用 observability 诊断**(search_observability auto 模式)是核心 agent-eng 故事
- ⚠️ **TokenAccountant 完全死代码**(零调用,cost_usd 恒 0)-> A6
- ⚠️ **structlog `bind_log_context` 从不调用**(trace_id/session_id 不进日志)-> A6
- ⚠️ **Langfuse 凭据未进 docker-compose doctor-api**(Docker 下 Langfuse 静默禁用)-> A6
- ⚠️ **Langfuse handler 可变单例**(并发下 trace 串台)-> A6
- ⚠️ 无跨系统 trace_id 链接(Langfuse↔Tempo 不可导航)-> B1
- ⚠️ doctor-api 日志不进 Loki(仅 stdout);Grafana dashboard 通用,无 agent 面板 -> B10
- ⚠️ trace_id 作 Loki stream label(高基数反模式);Langfuse v2(legacy);`record_llm_generation` 死代码

### 2.6 memory(长期记忆)
- ✅ `case_store.maybe_index_diagnosis` 写入侧(用户点赞触发);写侧三分离(embedding 只症状/根因语义,诊断输出进 payload,§4);**P1-a 双向量**:point 带 named vectors `symptom`+`root_cause`(批量 `embed_texts([symptom_passage, root_cause])` 单次往返)
- ✅ 检索侧 `case_retriever.search_historical_cases`(三因子 relevance×recency×importance + 自排 + trace 去重 + 阈值 + 空召回/异常降级,`using=symptom`)+ `_diagnosis_agent_node` 首次 pass §6.5 静态注入;HITL resume 从 `similar_cases_text` 缓存重注入不 re-query;`rag_injection_enabled` 开关;`retrieved_case_ids`/`similar_cases_text` 入 DoctorState -> A5 done
- ✅ **P1-a 双向量工具化检索**(§6.4,突破 #8 症状相似天花板):`case_retriever.search_by_root_cause(hypothesis)` 查 `root_cause` named vector(共享 `_search_named_vector` 管线,`using=root_cause`);包成 agent tool `search_historical_root_cause`(`src/tools/memory_recall.py`,agent 形成根因假设后主动调);独立开关 `rag_root_cause_tool_enabled`(与症状静态注入 `rag_injection_enabled` 解耦);Qdrant collection named vectors(`qdrant_client.NAMED_VECTORS`,per-vector INT8 quant,旧 schema 自动重建);`tools_reference.md`+`diagnosis_agent.j2` 加工具说明;注册成第 6 个工具。**before/after ablation(real bge-m3, recall@3)**:①1.00→1.00 / ②0.50→**1.00**(突破天花板✓,同根因异症状被召回)/ ③0.80→1.00(未降,已知限制:根因文本相似按"根因领域"聚类如后端 500 三连 BE-020/021/022,粗于机械根因身份)/ ④0.16→0.15(无噪声注入✓)。`scripts/eval_recall_ablation.py --vector both`。-> P1-a done
- ✅ **C hybrid 症状向量重构(已解决)**:`build_symptom_passage` 只编码 `user_report`(NL);`signal_types`/`tier` 走 Qdrant payload filter(tier 硬筛);`golden_signals.summary` 留 payload 不进向量,对齐 `code_search` 原则。payload `symptom_tier` 改用 `derive_tier(evidence)`(与 query filter 同源)。重建脚本 `scripts/rebuild_collection.py`(symptom 向量内容变,需重灌;部署期跑)。**eval 验收门已过**(real bge-m3, recall@3,2026-07-21):② 0.50->**1.00**(+0.50,达"理想 ②升"--移除 log 摘要让同根因异症状 case 不再被代码标识符推远,实证 C 核心假设)、③ 0.80(无回归;tier filter 对同症状异根因同 tier 无助,已知限制)、④ 0.16(无噪声);root_cause sanity 复现 P1-a(②1.00/③1.00/④0.15)。rebuild/live 留部署期。
- ✅ **P1-c 冲突检测**(§7.2):`detect_conflict` 在召回集(注入的 top-k)上检测 ≥2 distinct root_cause(文本归一化),`format_similar_cases` 注入"⚠️ 冲突提示:N 种不同根因,请勿锚定单一 case"防 top-1 锚定。冲突键选 root_cause 文本 distinctness(非 category 太粗漏 demo / 非向量受 §C ③ 限制),覆盖症状静态注入 + 根因工具两路径。限制:同 bug 异表述误报,靠 trace_id 去重+归一化缓解。单测 8 例。-> P1-c done
- ✅ 检索召回评测 `recall_ablation`(§9.1/§9.3):15 case 成对 cosine 四象限 recall@k,实证 P0 症状相似天花板(根因似症状异 BE-022↔FE-021 召回低 -> P1-a 突破基线);`recall_ablation.py` + `scripts/eval_recall_ablation.py`(`--vector {symptom,root_cause,both}`)+ 单测 -> #8 done
- ✅ 反馈回填 §8.1 闭合"越用越准"环:`case_store.backfill_effectiveness`(Qdrant retrieve->set_payload read-modify-write,effectiveness clamp[0,1],hit_count +1 on 👍,异常降级返回更新数)+ `feedback.py` upvote(👍 delta=+0.1/hit=True)接入,fire-and-forget(**👎 降权已移除**:归因不清会冤枉好 case,见设计 §8.1/§8.2;downvote 现只留结构化日志作 P1-b 失败 pattern 数据源);backfill 独立于新 case 索引成败(👍 认可诊断即认可召回参考);`_load_run_state` 增返 `retrieved_case_ids`;`_importance` 已读 hit_count/effectiveness -> 回填后自动生效;单测 backfill + feedback 流程 -> §8.1 done
- ✅ case_id 注入修复(闭合 §8.1 **live** 环):`bug_info_node` 从 `config["configurable"]["thread_id"]` 设 `case_id`/`trace_id`/`session_id`(if 未设,与 checkpointer 寻址同 key -> `case_id == checkpoint thread_id` 构造保证);图单一 owns,`_build_initial_state` 不再设。修 CopilotKit 路径 `state.case_id=None`(前端 👍 fallback 到 desync 的 `useCopilotContext().threadId` -> `feedback._load_run_state` 404)。注:`test_feedback.py` `_patch_graph` mock 图 -> §8.1 单测早绿但 live 👍 路径此前是断的;本次 live 验证通过。新增 `test_bug_info_sets_case_id_from_config_thread_id`
- (设计见 `long_term_memory_design.md`,权威)

### 2.7 security
- ✅ sql_guard app 层(见 2.2)
- ⚠️ "只读 role / SET TRANSACTION READ ONLY" **未实现**(以 postgres 超级用户连接;文档声称三层防御)
- ⚠️ `sanitize_path`/`sanitize_for_llm`/`safe_subprocess_args` 死代码;`file_reader` 手写重复沙箱未复用 `sanitize_path` -> B5

---

## 3. bug-factory

- ✅ **15 个 gold recipe**,8 类别:BE(020/021/022)、FE(020/021)、PERF(020/021)、LOGIC(020/021/022)、DATA(020/021)、RACE(020)、CONFIG(020)、CASCADE(020)
- ✅ recipe schema 丰富:`categories`(多标签)+ `cross_layer` + `symptom_tier`/`root_cause_tier` + `retrieval_gold` + `expected_evidence` + `expected_observation`(log_patterns/trace_attributes)
- ✅ injector:diff_patch 注入(确定性,`DiffPatchApplier`)+ git 分支管理
- ✅ trigger:每 case 注入 W3C `traceparent` 让后端采纳 + `ui_reachable` 门禁(强制经真实浏览器)+ `collect_diff`(无信号逻辑/数据/配置类 bug 用 `access_control_anomaly`/`silent_data_loss` 显式捕获)
- ⚠️ **AI rewriter 死代码路径**(15 recipe 全用 diff_patch,`injector.py:123` 让 patch 优先,`AIRewriter.rewrite_file` 永不触发;README 称"AI 自动生成 bug"不实)-> A10
- ⚠️ **`expected_observation` 从不校验**(bug 没真触发也能产出合法 case,eval ground truth 不可信)-> A9
- ⚠️ **不可复现**(trigger_trace_id 随机、user_report LLM 无 seed)-> A9
- ⚠️ `retrieval_gold` 无人消费(无 RetrievalEvaluator)-> B3

---

## 4. 评测 + scripts

- ✅ `scripts/langfuse_scorers.py`:7 维 scorer + process_quality(hosted,更丰富);`LLMJudgeEvaluator` 有 structured output + cache + fallback
- ✅ `score_category_accuracy` 主动防 gold 泄漏;`score_process_quality` 用 evidence_coverage 而非惩罚调用数
- ✅ 评分已单源(langfuse 7 维;benchmark 4 维 + run_case.py 5 维已移除)
- ⚠️ LLM judge 无自一致性(单次)+ 静默失败 `except: return 0.0`(与"诊断全错"不可区分)-> B2
- ⚠️ judge 模型隔离仅 local 路径(langfuse 路径回落到与 doctor 同模型)-> B2
- ⚠️ `score_trace.py:53-66` 有复制粘贴重复块

---

## 5. demo-app(被诊断目标)

- ✅ FastAPI + React(TaskFlow):auth(JWT)/项目 CRUD/任务 CRUD + 看板/@dnd-kit 拖拽/评论/Alembic 迁移/种子数据
- ✅ **bug-ready**:OTel tracing + 自定义 Loki bridge(注入 trace_id/span_id)+ FastAPI/SQLAlchemy instrumentation + 双通道错误上报(OTel span->Tempo 且 sendBeacon->Loki)+ W3C traceparent + `[TAG]` console 标记 + 结构化 diff 注入
- ⚠️ **`tasks.py`/`comments.py` 无 ownership 校验**(get/update/delete_task、create_comment 任意已认证用户可读写他人数据=pre-existing IDOR;而 logic_020 recipe 正是靠移除 owner 过滤注入 IDOR,baseline 不干净污染评测)-> A7
- ⚠️ `jwt_secret` 弱默认;alembic 外键约束名传 None

---

## 6. doctor 前端(支撑)

- ✅ **真展示 agent 工程**:`EvidenceChainGraph`(侦探板 4 列 signal->correlation->finding->report + 红线 + 点击聚焦)、`ToolCallCard`(编号步骤 + per-tool 图标 + 状态 + 可展开 args/result)、`BudgetPanel`(迭代环 + >80% 脉冲 + token in/out + 阶段 + 预算耗尽早停 banner)、`ReportPanel`、`parseAgentState`(防御性 JSON 抽取)
- ⚠️ EvalPage/RunPage/CasePage 占位("Phase 5 将实现")

---

## 7. infra

- ✅ docker-compose(10 服务:demo-fe/be、doctor-api、postgres、redis、grafana、loki、tempo、otel-collector、qdrant)+ Makefile + 多阶段 Dockerfile + CI(ruff/mypy strict/pytest)
- ⚠️ **无 K8s/Helm**(README 声称"K8s + Helm"不实)
- ⚠️ Langfuse v2(legacy);`tempo/config.yaml` metrics_generator 死配置;`init-db.sql` 实际为空
- ⚠️ `bug-factory/output/` 提交 9MB 生成证据(含全 traceback);`infra/tempo/data/`

---

## 8. 迭代历程要点(面试素材,详见 git log)

- baseline overall **0.598**(S0.2)-> iter2 **0.909**(单次,±0.03 方差,非定数)
- **Iteration 3**:手写 ReAct while 循环 -> `create_agent` + middleware(认识到手写循环维护成本,借 LangChain 1.0 middleware 找回预算/去重/forced call 注入点)
- **forced-call / structured-output 演进**:un-bound LLM + `function_calling` method(避 DeepSeek 对 json_schema 的 400)+ `include_raw=True` + Langfuse span 记录回调路径看不到的解析对象
- **S1.5**:iteration-based phase + 总时间帽 + 预算耗尽兜底报告 + 收敛检测
- **方法论**:每个机制由 case 跑分 + trace 分析驱动,非理论先行(harness-iteration-log 原 log 已删,git 可追溯)

---

## 9. 已知缺口汇总(详见 followup-plan P0)

声称✅但**未在跑**的项(面试官读代码会发现):
- RAG 只写不读(A5)、TokenAccountant/bind_log_context/Langfuse 凭据/handler 单例(A6)
- demo-app IDOR(A7)、source_map_resolve stub(A8)、bug 激活门禁+可复现(A9)、AI rewriter 死代码+三套评分(A10)

> 2026-07-16 收尾:A2(graph 测试路径修复)、A3(forced call `_last_ai_has_json` 守卫)、A4(上下文死代码删除 + `tool_result_truncation_enabled` 默认开)已完成,ruff + mypy strict + pytest 全绿。

---

## 10. 亮点(面试重点,勿当缺点)

1. **forced_call 结构化输出**--真懂 structured output 的坑(function_calling method 选择 + include_raw + span 记录)
2. **search_observability auto 模式**--agent 用 observability 诊断的核心故事(Loki->trace_id->Tempo->span 树->多类检测->因果链)
3. **evidence 管线**--确定性纯 Python、tiered、跨层关联,把便宜可复现的活留在 LLM 之外
4. **Langfuse 7 维 scorer + process_quality**--scorer 注释记录真实 trade-off 演进
5. **bug-factory recipe schema + traceparent 注入 + ui_reachable 门禁 + collect_diff**--eval-data 工程真功夫,跨层 bug 可评
6. **budget guard + ContextVar 每调用状态**--harness 工程真理解
7. **doctor 前端**--EvidenceChainGraph/ToolCallCard/BudgetPanel 真展示 agent 推理
8. **手写循环 -> create_agent+middleware 迁移**--比"一直手写"更强的成熟度故事
