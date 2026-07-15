# DiagDoctor 后续计划(2026-07-15 全项目审查产出)

> 本文由 2026-07-15 的全项目审查(文档 + 代码,6 组件并行深读)产出,聚焦**转 agent 岗**的简历价值。
>
> **与既有规划文档的关系**:
> - `next-directions.md`(2026-07-12):其 P0(历史诊断 RAG)已完成、P3(扩 30+ case)与「不做」决策冲突,**本文取代其作为当前路线**。
> - `diagdoctor-depth-directions-v2.md`(14 方向,pre-Phase-1 设计稿)、`diagdoctor-depth-handbook-v2.md`(执行手册 + S0/S1/S1.5 迭代日志):保留作设计理由 / 迭代记录。
> - 长期记忆系统以 `docs/long_term_memory_design.md` 为权威设计,**不在本文范围**;与之相关的 RAG 闭环仅作衔接项。
>
> 后续可与其他规划文档合并收敛(文档清理见审查报告 §二)。

---

## 核心原则

**最高优先级不是"加新功能",而是"把声称的能力做真"。**

审查发现一个系统性问题:README / docs / commit message 的声明与代码实际行为大面积脱节--大量标"✅ 已实现"的特性是**死代码 / 默认关闭 / 半成品**。agent 岗面试官一定会读代码,一旦发现"声称的能力没在跑",深度反而变负分(判断力 + 诚实度双扣)。

因此本计划分三层:
- **P0 必做**:故事对齐 / 除雷--让简历声明可信(几乎全是机械对齐,低成本高回报)。
- **P1 可选**:深化 agent 工程深度(中等成本,直接加强 agent 岗信号)。
- **不做**:过度工程或已决策放弃的方向。

原则:**宁"深化已有"勿"铺新摊子"**;全栈部分(doctor 前端、demo-app)仅作支撑,不追求打磨。

---

## P0 必做:故事对齐 / 除雷

这些不是"新能力",而是消除"声称 vs 代码"落差。每一项都是面试官读代码会立刻发现的雷。

| # | 方向 | 类别 | 简历价值 | 成本 | 动作 |
|---|---|---|---|---|---|
| A1 | 修 README + 文档对齐代码 | 文档 | ⭐⭐⭐⭐⭐ | 0.5d | 2 节点图(非 3 节点)、`engine/` 结构树(非 `graph/`)、"benchmark 保留作离线 replay"(非"已迁移")、删 K8s/Helm、删 docker-network-fixes 引用、替换 your-org、`context_engine.py`→`engine/context/`、限定 0.909 为单次 iter2、补文档索引遗漏 |
| A2 | 修坏掉的 graph 测试(`src.graph.*`→`src.engine.*`) | 测试 | ⭐⭐⭐⭐⭐ | 0.5d | reorg 后测试从没跑通(README 却称"CI 全绿")。最致命第一印象雷;修测试会顺带暴露 A3/A4 |
| A3 | forced call 改条件触发 | 编排 | ⭐⭐⭐⭐ | 0.5d | 接 `_last_ai_has_json` 守卫(函数存在且被测,但 middleware 未 import),或改"terminal structured-output 即设计"停止 prompt 索 JSON。当前每 case 多一次全历史 LLM 调用,健康运行零收益 |
| A4 | 上下文工程:接线或删 | 上下文工程 | ⭐⭐⭐⭐⭐ | 1d | `maybe_compact_context`/`build_dynamic_system_prompt` 接进 `BudgetGuardMiddleware.abefore_model`(可返回 `{"messages":...}`),截断默认开(`tool_result_truncation_enabled`→True);或删死代码 + 删 README ✅。当前"代码在没接线 + 文档✅"是最坏状态 |
| A5 | RAG 闭环(检索侧) | 记忆 | ⭐⭐⭐⭐⭐ | 1-1.5d | 实现 `case_retriever.search_historical_cases` + top-k 相似案例注入 diagnosis;当前只写不读(`case_store` 写入侧已做,检索侧零调用点),对诊断零影响。或诚实标"write-side only, retrieval pending" |
| A6 | 可观测死代码接线或删 | 可观测 | ⭐⭐⭐⭐⭐ | 1d | `TokenAccountant` 接线(callback + `MODEL_PRICING` + 进 DiagnoseResponse);`bind_log_context` 接 FastAPI middleware;Langfuse 凭据进 docker-compose doctor-api;Langfuse handler 改工厂(弃可变单例) |
| A7 | demo-app IDOR 修补 | 安全+评测 | ⭐⭐⭐⭐ | 0.5d | `tasks.py`/`comments.py` 的 get/update/delete_task、create_comment 加 ownership 检查(镜像 `list_tasks`)。pre-existing 漏洞既是安全雷,又污染受控注入评测(logic_020 正是靠移除 owner 过滤注入 IDOR) |
| A8 | `source_map_resolve` 实现或移除 | 工具 | ⭐⭐⭐ | 0.5d | 当前是 stub(原样返回 input + "passthrough"),却被 `inspect_frontend_error` 默认调用、工具描述宣传"Source map 还原"。模型会信输出在 minified 位置上推理 |
| A9 | bug-factory 激活门禁 + 可复现 metadata | 评测 | ⭐⭐⭐⭐ | 1d | 取证后校验 `expected_observation.log_patterns`(缺失标 invalid 不入库);case metadata 记 `generator_model`/`temperature`/`generation_seed` |
| A10 | 诚实重框 AI rewriter + benchmark 双评分 | 评测 | ⭐⭐⭐⭐ | 0.5d | README 改"AI 辅助 case 创作"(rewriter 备用于变体,gold 用确定性 diff patch);"benchmark 保留作离线 replay,Langfuse 为 canonical"。去掉两处故事雷 |

**P0 合计 ~6-7 天**,几乎全是机械对齐,但能把项目从"看起来没做完"拉到"明显迭代过且自己理解清楚"--面试可信度质变。

---

## P1 可选:深化 agent 工程深度

"把已有的做深",中等成本,直接加强 agent 岗信号。建议按面试想讲的方向挑 2-3 个,勿全做。

| # | 方向 | 类别 | 简历价值 | 成本 | 动作 |
|---|---|---|---|---|---|
| B1 | 跨系统 trace_id 链接 | 可观测 | ⭐⭐⭐⭐⭐ | 0.5d | `trigger_trace_ids[0]` 设为 doctor OTel span 属性 `diag.bug_trace_id` + Langfuse trace metadata。建立 Langfuse↔Tempo 可导航链接。**"端到端可观测"故事单点最高杠杆** |
| B2 | LLM judge 加固 | 评测 | ⭐⭐⭐⭐⭐ | 1.5d | 失败返 None 不返 0.0(当前 `except: return 0.0` 与"诊断全错"不可区分);最高权重维度(root_cause)k=3 自一致性;judge≠agent 强制隔离;5 case 人工一致性集报告 judge-human agreement |
| B3 | `RetrievalEvaluator`(消费 `retrieval_gold`) | 评测 | ⭐⭐⭐⭐ | 1d | 从 doctor 工具调用参数 / report `evidence_refs` 算 hit-rate 对 `retrieval_gold.code_chunks`。死字段变简历亮点指标;code_search 头牌能力终被量化 |
| B4 | Evidence Ranking | 上下文工程 | ⭐⭐⭐⭐ | 2-3d | 注入前按信息密度(ERROR/error span/时间距离/关键词)打分排序。可 ablation(同 token 预算下 accuracy/轮次对比) |
| B5 | 安全守卫接线 | 安全 | ⭐⭐⭐ | 1d | `sanitize_path` 进 file_reader/code_search(替换 file_reader 手写的重复沙箱);`sanitize_for_llm` 进 evidence 注入前。当前 4 个守卫中 3 个是死代码 |
| B6 | 统一工具返回/错误契约 | 工具 | ⭐⭐⭐ | 1d | 统一 envelope `{status,data,error?,hint?}` + `ToolError` 约定。当前 5 种不兼容错误形态,模型得逐工具学 |
| B7 | `DoctorState` 作真实图 schema | 编排 | ⭐⭐⭐ | 0.5d | graph 当前是 `StateGraph(dict)`,`DoctorState` 上声明的 reducer 根本没生效。`StateGraph(DoctorState)` 让 reducer 真跑,或删未用 reducer 并文档化 dict schema |
| B8 | 连续同工具调用上限(真 flailing 检测) | 编排 | ⭐⭐⭐ | 0.5d | 连续 N 次同工具→注入"已搜 N 次,总结再决策"nudge。兑现 commit "flailing 检测" 声明(当前只有语法去重 + 硬上限) |
| B9 | per-phase 成本归因 | 可观测 | ⭐⭐⭐ | 0.5d | `UsageRecord` 加 `phase`/`node`,`get_summary()` 出 `by_phase`。"我懂 agent 成本结构" vs "我记了 token" 的分水岭(依赖 A6 接线) |
| B10 | Grafana 加 agent 面板 | 可观测 | ⭐⭐⭐ | 1d | 评分趋势(查 langfuse Postgres `scores`)/ cost per diagnosis / tool-call count。当前 dashboard 只观测"病人"demo-app,不观测"医生"agent |

---

## 新方向(除长期记忆外)

长期记忆(RAG)已在规划内(A5/B3),不重复。以下是补强 agent 工程能力的新方向。

| # | 方向 | 类别 | 简历价值 | 成本 | 标注 | 说明 |
|---|---|---|---|---|---|---|
| C1 | 干净的 ablation harness | 评测 | ⭐⭐⭐⭐⭐ | 2d | 🔴 必做 | 同 case 集 × 配置开关(截断 on/off、RAG on/off、budget 上限)对比 runner,产出"配置 X 让 overall ±Y、token ±Z"表。**eval-driven dev 是 agent 岗核心信号,且让 B2/B3/B4 等都可量化验证**。杠杆支点 |
| C2 | CI 评测门禁 | 评测/工程 | ⭐⭐⭐⭐ | 2-3d | 🟡 可选 | GitHub Actions 跑 smoke case + overall 阈值门禁,三级(smoke/train/full)。依赖 C1 先有可信 baseline |
| C3 | 盲集隔离(gold 分 train/blind) | 评测 | ⭐⭐⭐ | 0.5d | 🟡 可选 | 防过拟合。`depth-directions §5.3.4` 已设计,小改 |
| C4 | few-shot 从检索案例注入 | 记忆/prompt | ⭐⭐⭐⭐ | 1d | 🟡 可选(依赖 A5) | 把 A5 检索结果结构化为 few-shot("相似已解 bug 的诊断路径")而非裸塞 `similar_cases`。闭合"越用越准" |
| C5 | 过程质量 evaluator 落到 benchmark | 评测 | ⭐⭐⭐ | 1d | 🟡 可选 | `score_process_quality` 已在 langfuse 侧跑通,回灌 benchmark 让离线路径也有过程分 |
| C6 | 诊断计划 TodoWrite | prompt/编排 | ⭐⭐⭐ | 1d | 🟡 可选 | 工具调用前先输出诊断计划,防漂移。prompt 改动为主 |
| C7 | 假设追踪与证伪 | 编排 | ⭐⭐⭐ | 2d | ⚪ 偏后 | 减确认偏误;依赖 B8/C6 先到位,且 15 case 难量化收益。建议 B8/C6 后再评估 |
| C8 | 持久化 checkpointer + DoctorState 真实 schema | 编排/状态 | ⭐⭐⭐⭐ | 1d | 🟡 可选(基础) | 现 MemorySaver 是内存态(重启丢失)、graph 用 dict 非 DoctorState(reducer 未生效)。换 SqliteSaver/PostgresSaver + B7 让检查点可跨重启保留。是 C9/C10 的前提 |
| C9 | 人机协同(HITL):ReAct 中断 + 人工补充信息 + 恢复 | 编排/交互 | ⭐⭐⭐⭐⭐ | 2d | 🟡 可选(高价值) | LangGraph `interrupt()` + `Command(resume=...)`;复用已集成的 CopilotKit(`render` 工具)做人工输入 UI。scope 收紧:中断点 + 一条人工引导消息 + 恢复,非完整协同编辑。依赖 C8 |
| C10 | 历史 bug case 诊断检查点恢复:保留/恢复未完成调查续查 | 编排/记忆 | ⭐⭐⭐⭐ | 1d | 🟡 可选 | 对某 bug case 的诊断调查若未完成(预算耗尽/被中断),不丢工作,从 checkpoint 恢复续查(可叠加 C9 人工引导)。与 budget guard 故事呼应。依赖 C8/C9 |

### 人机协同与检查点恢复(C8-C10,新增)

**值得做吗?--值得,且是本项目最契合「深化已有」的方向之一。**

理由:
- **复用已有基建,非新摊子**:项目已集成 CopilotKit(其核心价值就是 HITL + 流式 agent state)、已用 MemorySaver checkpointer、doctor 前端已流式展示 tool call / budget / evidence chain。C8-C10 是把这些「半成品」做实做深,而非从零造轮子。
- **主流 agent 工程深度**:LangGraph `interrupt` / `Command(resume)` + 持久化 checkpointer 是 HITL 标准模式;Anthropic 工具调用暂停确认、CopilotKit `render` 人工输入均为业界常态。面试可讲清中断-恢复-状态持久化机制。
- **故事自洽 + 杀手级 demo**:与现有 budget guard 串成闭环--「agent 预算耗尽 → 暂停 → 人工补一句『查下迁移脚本』→ 从 checkpoint 恢复、带新预算续查 → 完成」。一个 demo 同时展示 budget 治理 + 检查点 + HITL,连贯且不堆砌。
- **依赖链清晰**:C8(持久化 checkpointer + 真实 DoctorState)是地基 → C9(HITL)与 C10(历史恢复)是它解锁的两个能力。故 C8 依赖 B7,C9/C10 依赖 C8。

**两个诚实边界(需讲清,避免被当过度工程)**:
1. **必须在 P0 之后**:在上下文工程死代码 + 测试坏掉 + dict 非 DoctorState 的地基上建 HITL/恢复不可靠。先做 A2 / A4 / B7 / C8。
2. **15-case headless benchmark 无法量化 HITL 价值**:benchmark 无人介入跑分,HITL 的价值在交互 demo + 面试叙事,不在 overall 分。这与 RAG「边界判断」同理--架构正确但当前场景难量化,面试讲清即可。HITL 走 CopilotKit 交互路径,benchmark 走 headless 路径,两者并存不冲突。

---

## 明确不做(过度工程 / 已决策)

| 方向 | 原因 |
|---|---|
| Bug case 扩到 30+ | `depth-directions-v2 §8` 已决策不做;15 case 跨 8 类对简历够。`next-directions P3` 与之冲突,以"不做"为准 |
| Bug 变异引擎 | 同上,已决策不做 |
| 多 agent 回归(Triage/Specialist/Critic) | V3 有意收敛为单 agent,尊重该决策;把单 agent 做好而非铺摊子 |
| K8s + Helm 部署 | 超出 side project 定位;面试讲设计思路即可。README 该删此声明 |
| 全功能 prompt 管理平台(版本/ablation/eval 绑定全套) | 过度工程;registry 加 `REQUIRED_VARS` + Langfuse 记版本 hash 即够 |
| 四阶段动态策略注入 | 实测无收益,已放弃(正确决策) |
| 多租户/生产部署 | 超出定位 |

---

## 推荐执行顺序(简历 ROI 最大化)

### 第一波:对齐除雷(~1 周)
**A1 → A2 → A10 → A8 → A7 → A6 → A3 → A4 → A5 → A9**

先把"声称 vs 代码"落差清干净。A2 优先(测试坏掉是第一印象雷),A1/A10 并行(文档),A5 单列(RAG 闭环是最大单点)。
**这波做完,项目从"看起来没做完"变"明显迭代且自洽"--面试可信度质变。**

### 第二波:深化评测(~1 周)
**C1 → B2 → B3 → C3**

C1(ablation harness)是杠杆支点--它让 B2(judge 加固)、B3(retrieval eval)、B4(evidence ranking)都变得**可量化验证**。没有 C1,所有"可选"都是空口。agent 岗核心信号 = eval-driven dev。

### 第三波:深化可观测 + 编排(按兴趣挑 2-3)
**B1 → B6 → B7 → B8 → B9 → B10 → C4 → C6**

B1(跨系统 trace_id)单点最高杠杆。其余按面试想讲的方向挑,勿全做。

**备选第四轨(若想主打交互式 agent)**:C8 -> C9 -> C10。人机协同 + 检查点恢复复用 CopilotKit 基建、杀手级 demo 连贯,是「深化已有」的强信号;但须在 P0 + B7 之后,且价值在 demo/叙事而非 benchmark 分。

---

## 附:面试重点讲的真亮点(勿当缺点)

审查中反复确认的真深度。讲清这些 + 把 P0 的雷清掉,故事就圆了:

1. **`forced_call` 结构化输出**:`function_calling` method 选择(默认 `json_schema` 被 DeepSeek 400)+ `include_raw` + Langfuse span 记录解析对象--真懂 structured output 的坑。
2. **`search_observability` auto 模式**:Loki→trace_id→Tempo→跨层 span 树→N+1/bottleneck/cascade/timeout 检测→因果链→insight--agent 用 observability 诊断的核心故事。
3. **evidence 管线**:确定性纯 Python、tiered、跨层关联、golden signal--把便宜可复现的活留在 LLM 之外的正确取舍。
4. **Langfuse 7 维 scorer + process_quality**:scorer 注释记录真实 trade-off 演进(为何弃用朴素 dedup/budget ratio)--"学到的取舍"最读得通。
5. **bug-factory recipe schema + traceparent 注入 + ui_reachable 门禁 + collect_diff**:eval-data 工程真功夫,跨层 bug 可评是差异化。
6. **budget guard + ContextVar 每调用状态**:harness 工程真理解(`jump_to="end"`、middleware 无状态)。
7. **doctor 前端**:EvidenceChainGraph / ToolCallCard / BudgetPanel 真展示 agent 推理,非通用图表。
8. **手写循环 → create_agent+middleware 迁移**(Iteration 3):**别藏,这比"一直手写"是更强的成熟度故事**。
