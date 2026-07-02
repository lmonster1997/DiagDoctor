# DiagDoctor 深度化方向规划

> 本文档系统性地梳理 DiagDoctor 在当前 V3 架构基础上可以进一步深化的方向。
> 每个方向包含：现状分析、目标、具体实现方案、代码级改动点、优先级评估。
>
> **核心原则**：深度 ≠ 复杂。深度的标志是「在相同输入下，诊断准确率更高、
> 证据链更完整、修复建议更可执行」。

---

## 目录

| # | 方向 | 核心价值 | 优先级 |
|---|------|---------|--------|
| 0 | 手动 Agent 循环 | 所有 harness 机制的前提 | P0 |
| 1 | Ingest 层：证据归一化管线深度化 | 提升信号纯度，减少 Agent 噪声 | P0 |
| 2 | search_observability：自动分析模式深度化 | 从"查数据"到"给洞察" | P0 |
| 3 | code_search：从向量检索到 ripgrep 混合检索 | 精确匹配 >> 语义搜索 | P0 |
| 4 | 上下文工程：压缩、预算、动态策略 | Agent 推理质量的地基 | P0 |
| 5 | 评测体系：从"打分"到"诊断质量门禁" | 可量化、可回归 | P1 |
| 6 | System Prompt 工程：从"指令"到"策略" | Agent 决策质量上限 | P1 |
| 7 | 诊断计划（TodoWrite） | 先规划再执行，防止漂移 | P1 |
| 8 | Bug Factory：从"静态配方"到"变异生成" | 评测覆盖面 | P1 |
| 9 | 安全沙箱：从"单点防护"到"纵深防御" | 生产可用性 | P2 |
| 10 | Agent 自省与纠错机制 | 减少幻觉和误诊 | P2 |
| 11 | 成本优化与模型路由 | 运行经济性 | P2 |
| 12 | Hook 系统：工具调用扩展点 | 不修改主循环的扩展能力 | P2 |
| 13 | Subagent 上下文隔离 | 复杂 case 的 context 隔离 | P2 |

---

## 0. 手动 Agent 循环：从 `create_agent` 黑盒到自控循环

> **这是整个深度化计划的第一步——所有其他 harness 机制的前提。**

### 0.1 现状

当前 `unified_agent_node` 直接调用 `create_agent().ainvoke()`：

```python
# graph/nodes/unified_agent.py — 现状
agent = get_unified_agent()  # create_agent() 构建的编译图
agent_result = await agent.ainvoke({"messages": [HumanMessage(...)]})
# ↑ 这一行进去，里面发生了什么你完全管不了
```

`create_agent()` 内部实现了 ReAct 循环（调 LLM → 解析 tool_calls → 执行工具 → 回传结果 → 再调 LLM），但**循环过程对外不可见**。无法在"工具结果进入 messages 之前"做截断，无法在"每次迭代前"检查预算，无法注入 Hook。

### 0.2 核心问题

learn-claude-code 的核心原则是 *"The loop belongs to the agent. The mechanisms belong to the harness."* 但前提是**你拥有循环的控制权**。`create_agent().ainvoke()` 把循环封装在 LangGraph 内部，你失去了所有注入点：

| 想做的机制 | 需要 | 当前能否实现 |
|-----------|------|-------------|
| 工具结果截断 | 工具执行后、入 messages 前 | ❌ 无法插入 |
| 上下文压缩 | 每次迭代前检查预算 | ❌ 无法插入 |
| Hook（PreToolUse/PostToolUse） | 工具执行前后 | ❌ 无法插入 |
| 动态 System Prompt | 每次迭代重新组装 | ❌ prompt 在构建时固定 |
| Decision Trace | 每步记录推理 | ❌ 无法访问中间状态 |
| 工具调用去重 | 工具执行前检查历史 | ❌ 无法插入 |

### 0.3 目标

将内层 Agent 循环从 `create_agent` 拿出来，自己写 `while` 循环。**外层 LangGraph 图拓扑不变**（ingest → unified_agent → reporter），只是 `unified_agent` 节点内部从"调黑盒"改为"自控循环"。

### 0.4 具体方案

```python
# doctor/src/graph/nodes/unified_agent.py — 改后核心结构

async def unified_agent_node(state: DoctorState) -> dict[str, Any]:
    """LangGraph node: 手动驱动的统一诊断 Agent。"""
    evidence: NormalizedEvidence = state.evidence
    evidence_text = format_evidence_for_agent(evidence)

    # 构建初始 messages
    base_prompt = _build_system_prompt()
    messages: list[BaseMessage] = [
        SystemMessage(content=base_prompt),
        HumanMessage(content=evidence_text),
    ]

    # 获取 LLM 和工具
    llm = get_llm_for_role("diagnosis")
    tools = get_all_tools()
    tool_map = {t.name: t for t in tools}

    # 工具调用去重缓存
    call_history: list[tuple[str, str]] = []

    # 手动驱动 Agent 循环
    for iteration in range(MAX_TOOL_CALLS):
        # ✅ 注入点 1：预算检查 + 上下文压缩（方向 4）
        # messages = maybe_compact_context(messages, budget)

        # ✅ 注入点 2：动态 System Prompt（方向 6）
        # if budget.phase == FINALIZING:
        #     messages.append(HumanMessage("⚠️ 预算即将耗尽，立即输出结论"))

        # 调用 LLM（带 tools，和 create_agent 内部做的一样）
        response: AIMessage = await llm.ainvoke(messages, tools=tools)
        messages.append(response)

        # 无工具调用 → Agent 输出最终结果
        if not response.tool_calls:
            break

        # 执行工具调用
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]

            # ✅ 注入点 3：工具调用去重
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True))
            if call_key in call_history:
                messages.append(ToolMessage(
                    content=f"[跳过：与之前调用完全相同]",
                    tool_call_id=tc["id"],
                    name=tool_name,
                ))
                continue
            call_history.append(call_key)

            # ✅ 注入点 4：PreToolUse Hook（方向 12）
            # args = await registry.run_pre(tool_name, tool_args)

            # 执行工具
            try:
                result = await tool_map[tool_name].ainvoke(tool_args)
            except Exception as exc:
                result = f"工具执行错误: {exc}"

            # ✅ 注入点 5：PostToolUse Hook（方向 12）
            # result = await registry.run_post(tool_name, result)

            # ✅ 注入点 6：工具结果截断（方向 4）
            # result = truncate_tool_result(tool_name, str(result))

            messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tc["id"],
                name=tool_name,
            ))

            # ✅ 注入点 7：Decision Trace 记录
            # recorder.record_tool_call(iteration, tool_name, tool_args, result)

    # 解析输出
    report = parse_diagnosis_report({"messages": messages})
    findings = extract_findings({"messages": messages})
    # ... 后续逻辑不变
```

### 0.5 LangGraph 保留什么

| 组件 | 保留？ | 原因 |
|------|--------|------|
| 外层图拓扑（ingest→agent→reporter） | ✅ | 节点编排、状态流转 LangGraph 做得好 |
| `DoctorState` 状态定义 | ✅ | Pydantic + LangGraph 集成良好 |
| `MemorySaver` checkpoint | ✅ | 诊断中断恢复有用 |
| `create_agent()` | ❌ | 黑盒，换成手动 while 循环 |
| LangChain 的 `BaseChatModel` + `tools` | ✅ | LLM 调用和工具定义仍用 LangChain |

### 0.6 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 手动循环骨架 | 极高——所有 harness 机制的前提 | 1d | **P0** |
| 工具调用去重 | 高——20 行代码，立即生效 | 0.5d | **P0** |
| 工具执行错误不中断 | 高——鲁棒性基础 | 0.5d | **P0** |

> **关键原则**：不是抛弃 LangGraph，而是不用 `create_agent` 这个封装。外层图用 LangGraph 编排，内层 Agent 循环自己写——这样你才能在循环中注入上下文压缩、Hook、预算控制、Decision Trace 这些 harness 机制。

---

## 1. Ingest 层：证据归一化管线深度化

### 1.1 现状

当前 Ingest 管线（`doctor/src/ingest/normalizer.py`）已实现 9 步流水线：

```
tier_aware → denoise → dedup_and_fold → build_cross_tier_tree
→ merge_timeline → extract_golden_signals → correlate_by_trace_id
→ compute_noise_ratio → build_raw_refs
```

**已有能力**：
- 前后端 tier 标记（`tier_aware.py`）
- 噪声过滤：`/health`、`/metrics`、INFO 级 HTTP 请求（`denoiser.py`）
- N+1 折叠：相同 normalized message ≥3 次时折叠为 `[×N]`（`deduplicator.py`）
- 跨层关联：trace_id 链式关联前端→后端→DB（`correlator.py`）
- 黄金信号提取：error_log / error_span / slow_span / browser_error（`signal_extractor.py`）

**不足**：
1. N+1 折叠仅基于 log message 文本归一化，不识别 trace span 级别的 N+1
2. 噪声过滤是静态 pattern 匹配，无法适应不同应用的噪声特征
3. 信号提取没有置信度评分——所有 ERROR 级日志权重相同
4. 没有"烟雾弹"检测——逻辑/数据类 Bug 的日志/Trace 全部正常，Ingest 层不产生任何信号

### 1.2 目标

让 Ingest 层从"数据清洗工"升级为"证据分析师"：输出的不仅是清洗后的数据，还有**结构化的诊断线索和置信度**。

### 1.3 具体方案

#### 1.3.1 Span 级 N+1 检测（当前仅 log 级）

**问题**：`deduplicator.py` 只折叠 log message，但 N+1 的真正证据在 trace span 里——同一个 SQL 语句的 span 重复出现 N 次。

**方案**：在 `signal_extractor.py` 中增加 span 级 N+1 检测：

```python
# signal_extractor.py 新增

def _detect_span_n_plus_one(spans: list[dict]) -> Signal | None:
    """检测 trace span 中的 N+1 模式。

    判定条件：
    1. 同一个 db_statement（归一化后）出现 ≥3 次
    2. 这些 span 的 parent_span_id 相同（同一个父调用）
    3. 总耗时 = 单次耗时 × 次数（线性增长特征）
    """
    # 按 (normalized_sql, parent_span_id) 分组
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for span in spans:
        sql = span.get("db_statement", "")
        if not sql:
            continue
        normalized = _normalize_sql(sql)
        parent = span.get("parent_span_id", "")
        groups[(normalized, parent)].append(span)

    for (sql, parent), group in groups.items():
        if len(group) >= 3:
            total_ms = sum(s.get("duration_ms", 0) for s in group)
            return Signal(
                signal_id=f"sig-n1-{_short_id()}",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="warning",
                summary=f"N+1 检测：'{sql[:80]}' 重复 {len(group)} 次，总耗时 {total_ms:.0f}ms",
                evidence_ref=group[0].get("span_id", ""),
                metadata={
                    "query": sql[:200],
                    "count": len(group),
                    "total_duration_ms": total_ms,
                    "parent_span_id": parent,
                },
            )
    return None
```

**改动文件**：`doctor/src/ingest/signal_extractor.py`

#### 1.3.2 证据置信度评分

**问题**：当前所有 Signal 的 severity 只有 error/warning/info 三档，Agent 无法区分"确定性根因线索"和"可能是噪声的 ERROR 日志"。

**方案**：为每个 Signal 增加 `confidence: float` 字段（0.0~1.0），评分规则：

| 信号特征 | 置信度加成 |
|---------|-----------|
| 有 trace_id 且 trace 中有 error span | +0.3 |
| 有跨层关联（correlation 中存在） | +0.2 |
| ERROR 级日志且包含 stack trace | +0.2 |
| 日志来自业务服务（非 middleware） | +0.1 |
| browser_error 且有 source map 定位 | +0.3 |
| slow_span 且有 db_statement | +0.2 |
| 信号时间在用户报告时间窗口 ±5min 内 | +0.1 |

**基础置信度**：error=0.5, warning=0.3, info=0.1，叠加上述加成后 clamp 到 [0, 1]。

**改动文件**：
- `doctor/src/graph/state.py`：`Signal` 增加 `confidence: float = 0.5` 字段
- `doctor/src/ingest/signal_extractor.py`：提取信号时计算置信度
- `doctor/src/graph/nodes/unified_agent.py`：`format_evidence_for_agent()` 按置信度排序展示

#### 1.3.3 自适应噪声过滤

**问题**：`denoiser.py` 的 `_BACKEND_NOISE_PATTERNS` 是硬编码列表，换一个应用就得改代码。

**方案**：基于统计的自适应噪声检测——在 denoise 之前先做一轮统计扫描：

```python
# denoiser.py 新增

def _compute_log_frequency_stats(logs: list[dict]) -> dict[str, float]:
    """统计每类日志（按 normalized message 分组）的出现频率。

    频率 > 阈值的消息模板被标记为噪声候选。
    """
    template_counts: Counter[str] = Counter()
    for log in logs:
        msg = str(log.get("message", ""))
        normalized = _normalize_log_message(msg)
        template_counts[normalized] += 1

    total = sum(template_counts.values())
    return {
        template: count / total
        for template, count in template_counts.items()
        if count / total > 0.05  # 占比 >5% 的模板
    }


def adaptive_denoise(logs: list[dict]) -> list[dict]:
    """自适应噪声过滤：高频低信息量日志自动降权。"""
    noise_templates = _compute_log_frequency_stats(logs)
    return [
        log for log in logs
        if _normalize_log_message(str(log.get("message", ""))) not in noise_templates
        or _get_level(log).upper() not in ("INFO", "DEBUG")
    ]
```

**改动文件**：`doctor/src/ingest/denoiser.py`

#### 1.3.4 "烟雾弹"检测——无信号场景标记

**问题**：逻辑/数据/配置类 Bug（如 IDOR 越权、排序错误、JWT 过期配置错误）的日志和 Trace 全部正常（200 OK），`extract_golden_signals()` 返回空列表。当前 Agent 收到"无信号"提示后不知道该怎么行动。

**方案**：在 Ingest 层增加"无信号"场景的主动标记和策略提示：

```python
# normalizer.py 中 extract_golden_signals 之后

if not signals:
    # 无错误信号 → 标记为"烟雾弹"场景
    # 根据 user_report 关键词推断可能的 Bug 类型
    report_lower = (evidence.user_report or "").lower()
    if any(kw in report_lower for kw in ["越权", "别人的", "不应该", "idor"]):
        signals.append(Signal(
            signal_id=f"sig-smokeless-{_short_id()}",
            source="user_report",
            signal_type="smokeless_logic",
            severity="info",
            summary="无错误信号，但用户报告暗示逻辑/权限问题。建议：检查 API 端点的权限过滤条件",
            confidence=0.4,
        ))
    elif any(kw in report_lower for kw in ["排序", "顺序", "不对", "错误的数据"]):
        signals.append(Signal(
            signal_id=f"sig-smokeless-{_short_id()}",
            source="user_report",
            signal_type="smokeless_data",
            severity="info",
            summary="无错误信号，但用户报告暗示数据问题。建议：检查排序逻辑、字段映射、数据转换",
            confidence=0.4,
        ))
```

**改动文件**：`doctor/src/ingest/normalizer.py`、`doctor/src/ingest/signal_extractor.py`

#### 1.3.5 多级跨层关联——不依赖 trace_id 的兜底机制

> **背景**：企业开发中，trace_id 一致性远比理想情况差。当前 correlator 仅按 trace_id 分组，
> 但以下场景 trace_id 不会匹配：
>
> | 场景 | trace_id 一致性 | 原因 |
> |------|----------------|------|
> | 后端 API → DB 查询 | ✅ 高 | 同一请求上下文，OTel 自动传播 |
> | 微服务间调用（A→B） | ⚠️ 中 | 需配置 W3C Trace Context |
> | 前端 fetch → 后端处理 | ⚠️ 中 | 需前端注入 traceparent header |
> | **前端渲染崩溃 → 后端 API** | ❌ **极低** | 崩溃发生在 React 渲染生命周期，不在 HTTP 请求中；错误上报是独立请求，生成新 trace_id |
>
> 典型案例 FE-020：浏览器 TypeError 的 trace_id（`45fe31b3...`）与后端 `GET /api/tasks` 的
> trace_id（`a9c70986...`）完全不同。如果仅依赖 trace_id 关联，`correlations` 为空，
> 导致 `has_cross_layer=False`，Agent 走 `frontend_crash` 策略而非 `cross_layer_crash`，
> 最终只给"加可选链"的症状补丁（llm_judge ≤ 0.4）。

**方案**：在 `correlator.py` 中实现多级关联策略，trace_id 为主关联键，增加三层兜底：

```python
# correlator.py 新增

def correlate_multi_level(
    logs: list[dict],
    traces: list[dict],
    browser_errors: list[dict],
    golden_signals: list[Signal],
    user_report: str = "",
    time_window_s: float = 5.0,
) -> list[Correlation]:
    """多级跨层关联：trace_id → URL 路径 → 时间窗口 → 错误语义。

    优先级：
    1. trace_id 匹配（现有逻辑，后端内部场景有效）
    2. URL 路径匹配 + 时间窗口邻近（前端崩溃 → 后端 API）
    3. 错误语义推断（TypeError: undefined → 疑似字段缺失）
    4. 用户报告语义（最后兜底）
    """
    correlations: list[Correlation] = []

    # Level 1: trace_id 关联（现有逻辑）
    correlations.extend(correlate_by_trace_id(logs, traces, browser_errors, golden_signals))

    # 如果 trace_id 已找到跨层关联，不需要兜底
    has_cross_layer = any(
        c.frontend_signals and c.backend_signals for c in correlations
    )
    if has_cross_layer:
        return correlations

    # Level 2: URL 路径 + 时间窗口关联
    # 前端崩溃的 component_stack 中可能包含页面路径（如 TaskBoardPage），
    # 后端日志中有 API 路径（如 GET /api/projects/{id}/tasks）。
    # 如果前端崩溃时间与后端 API 调用时间差 < time_window_s，推断关联。
    url_correlations = _correlate_by_url_and_time(
        logs, traces, browser_errors, golden_signals, time_window_s
    )
    correlations.extend(url_correlations)

    # Level 3: 错误语义推断
    # TypeError: Cannot read properties of undefined (reading 'X')
    # → 自动搜索时间窗口内返回 200 的 API 响应，标记"疑似字段 X 缺失"
    semantic_correlations = _correlate_by_error_semantics(
        logs, traces, browser_errors, golden_signals, time_window_s
    )
    correlations.extend(semantic_correlations)

    return correlations


def _correlate_by_url_and_time(
    logs: list[dict],
    traces: list[dict],
    browser_errors: list[dict],
    signals: list[Signal],
    time_window_s: float,
) -> list[Correlation]:
    """通过 URL 路径推断和时间窗口邻近关联前端崩溃与后端 API。"""
    correlations = []

    for err in browser_errors:
        err_time = _parse_timestamp(err.get("timestamp", ""))
        if not err_time:
            continue

        # 从 component_stack 或 message 中提取页面/组件信息
        component_info = str(err.get("component_stack", "")) + str(err.get("message", ""))

        # 查找时间窗口内的后端 API 调用
        nearby_api_logs = []
        for log in logs:
            log_time = _parse_timestamp(log.get("timestamp", ""))
            if not log_time:
                continue
            delta = abs((log_time - err_time).total_seconds())
            if delta <= time_window_s:
                log_msg = str(log.get("message", log.get("line", "")))
                # 只关联业务 API 调用（排除 /api/log/client-error 等上报本身）
                if "GET" in log_msg or "POST" in log_msg:
                    if "/api/log/" not in log_msg and "OPTIONS" not in log_msg:
                        nearby_api_logs.append((log, delta))

        if nearby_api_logs:
            # 取时间最近的 API 调用
            nearby_api_logs.sort(key=lambda x: x[1])
            closest_log, delta = nearby_api_logs[0]
            api_msg = str(closest_log.get("message", closest_log.get("line", "")))

            correlations.append(Correlation(
                correlation_id=f"corr-url-{_short_id()}",
                trace_id=None,  # 无 trace_id 关联
                description=(
                    f"前端崩溃 → 后端 API（时间差 {delta:.1f}s）：{api_msg[:150]}"
                ),
                frontend_signals=[s.signal_id for s in signals if s.service_tier == "frontend"],
                backend_signals=[s.signal_id for s in signals if s.service_tier == "backend"],
                db_signals=[],
                confidence=0.5,  # 低于 trace_id 关联的 0.8
                correlation_method="url_time_proximity",
            ))

    return correlations


def _correlate_by_error_semantics(
    logs: list[dict],
    traces: list[dict],
    browser_errors: list[dict],
    signals: list[Signal],
    time_window_s: float,
) -> list[Correlation]:
    """通过错误消息语义推断跨层关联。

    检测模式：
    - TypeError: Cannot read properties of undefined (reading 'X')
      → 疑似后端 API 响应缺少字段 X
    - TypeError: X is not a function
      → 疑似 API 返回类型与前端预期不符
    """
    import re

    UNDEFINED_PATTERN = re.compile(
        r"Cannot read properties of undefined \(reading '(\w+)'\)"
    )

    correlations = []

    for err in browser_errors:
        msg = str(err.get("message", ""))
        match = UNDEFINED_PATTERN.search(msg)
        if not match:
            continue

        missing_field = match.group(1)
        err_time = _parse_timestamp(err.get("timestamp", ""))
        if not err_time:
            continue

        # 查找时间窗口内返回 200 的 API 调用
        for log in logs:
            log_time = _parse_timestamp(log.get("timestamp", ""))
            if not log_time:
                continue
            delta = abs((log_time - err_time).total_seconds())
            if delta > time_window_s:
                continue

            log_msg = str(log.get("message", log.get("line", "")))
            if "200" in log_msg and ("GET" in log_msg or "POST" in log_msg):
                if "/api/log/" in log_msg or "OPTIONS" in log_msg:
                    continue

                correlations.append(Correlation(
                    correlation_id=f"corr-sem-{_short_id()}",
                    trace_id=None,
                    description=(
                        f"疑似 API 契约缺陷：前端读取 '{missing_field}' 时 undefined，"
                        f"附近 API 返回 200：{log_msg[:120]}"
                    ),
                    frontend_signals=[s.signal_id for s in signals if s.service_tier == "frontend"],
                    backend_signals=[s.signal_id for s in signals if s.service_tier == "backend"],
                    db_signals=[],
                    confidence=0.6,
                    correlation_method="error_semantics",
                    metadata={"suspected_missing_field": missing_field},
                ))
                break  # 一个错误只关联一个 API

    return correlations
```

**Correlation 模型需扩展**：

```python
# state.py Correlation 增加
class Correlation(BaseModel):
    # ...existing fields...
    correlation_method: str = "trace_id"  # trace_id | url_time_proximity | error_semantics
    metadata: dict[str, Any] = Field(default_factory=dict)
```

**改动文件**：
- `doctor/src/ingest/correlator.py`：新增 `correlate_multi_level()` 及两个兜底函数
- `doctor/src/graph/state.py`：`Correlation` 增加 `correlation_method` 和 `metadata` 字段
- `doctor/src/ingest/normalizer.py`：调用 `correlate_multi_level()` 替代 `correlate_by_trace_id()`

### 1.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| Span 级 N+1 检测 | 高——直接提升 PERF 类 Bug 诊断 | 0.5d | P0 |
| 证据置信度评分 | 高——Agent 决策质量直接受益 | 1d | P0 |
| 自适应噪声过滤 | 中——当前 15 个 case 够用 | 1d | P1 |
| 烟雾弹检测 | 高——LOGIC/DATA/CONFIG 类 Bug 的关键入口 | 0.5d | P0 |
| **多级跨层关联** | **极高——跨层 Bug 诊断的前提，企业现实必需** | **1.5d** | **P0** |

---

## 2. search_observability：自动分析模式深度化

### 2.1 现状

`doctor/src/tools/observability_unified.py` 的 `search_observability` 工具支持 `source="auto"` 模式：

```
auto 模式流程：Loki 查日志 → 提取 trace_id → Tempo 查 trace → 分析
```

**已有能力**：
- 自动从日志中提取 trace_id（32 位 hex 正则匹配）
- 时间范围校验（最大 4 小时窗口）
- 日志条数限制（AUTO_MODE_LOG_LIMIT=500）
- trace_id 数量限制（AUTO_MODE_MAX_TRACE_IDS=5）

**不足**：
1. 分析阶段只是简单汇总（错误数、慢 span 数），不做异常检测
2. 多 trace 之间不做关联分析（如多个请求共享同一个慢 DB 查询）
3. 返回的是原始数据摘要，Agent 还需要自己推理因果链

### 2.2 目标

让 `search_observability` 从"数据查询工具"升级为"洞察生成工具"——返回的不仅是数据，还有**结构化的分析结论和因果链假设**。

### 2.3 具体方案

#### 2.3.1 异常检测层

**方案**：在 auto 模式的分析阶段增加异常检测：

```python
# observability_unified.py 新增

def _detect_anomalies(
    logs: list[dict],
    traces: list[dict],
) -> list[dict]:
    """从日志和 trace 中检测异常模式。

    检测规则：
    1. 错误突增：某时间窗口内 ERROR 日志数 > 均值 + 2σ
    2. 延迟突增：某 span 的 duration > 同类 span 均值 × 3
    3. 错误聚类：相同错误消息在短时间内重复出现（burst）
    4. 级联失败：一个 trace 内多个 span 同时报错
    5. 超时链：span 链路上最后一个 span 耗时占比 > 80%
    """
    anomalies = []

    # --- 错误突增检测 ---
    error_timestamps = [
        log.get("timestamp") for log in logs
        if _get_level(log).upper() == "ERROR"
    ]
    if len(error_timestamps) >= 5:
        buckets = _bucket_by_minute(error_timestamps)
        mean_count = statistics.mean(buckets.values())
        std_count = statistics.stdev(buckets.values()) if len(buckets) > 1 else 0
        for minute, count in buckets.items():
            if count > mean_count + 2 * std_count:
                anomalies.append({
                    "type": "error_burst",
                    "description": f"错误突增：{minute} 有 {count} 条 ERROR（均值 {mean_count:.1f}）",
                    "severity": "high",
                })

    # --- 延迟突增检测 ---
    span_durations: dict[str, list[float]] = defaultdict(list)
    for span in traces:
        name = span.get("name", "unknown")
        dur = float(span.get("duration_ms", 0))
        span_durations[name].append(dur)

    for name, durations in span_durations.items():
        if len(durations) >= 3:
            mean_dur = statistics.mean(durations)
            for dur in durations:
                if dur > mean_dur * 3 and dur > 200:
                    anomalies.append({
                        "type": "latency_spike",
                        "description": f"延迟突增：{name} 耗时 {dur:.0f}ms（同类均值 {mean_dur:.0f}ms）",
                        "severity": "medium",
                        "span_name": name,
                        "duration_ms": dur,
                    })

    # --- 级联失败检测 ---
    trace_spans: dict[str, list[dict]] = defaultdict(list)
    for span in traces:
        tid = span.get("trace_id", "")
        if tid:
            trace_spans[tid].append(span)

    for tid, spans in trace_spans.items():
        error_spans = [s for s in spans if s.get("status") == "error"]
        if len(error_spans) >= 2:
            anomalies.append({
                "type": "cascade_failure",
                "description": f"级联失败：trace {tid[:8]}... 内有 {len(error_spans)} 个 error span",
                "severity": "high",
                "trace_id": tid,
            })

    # --- 契约异常检测（200 OK 但响应可能有问题）---
    # 企业现实中，相当一部分根因是"静默的"——后端返回 200 但响应缺少字段、
    # 类型不匹配或数据不完整。这类问题不会触发 error/slow 信号，但会导致
    # 前端崩溃或数据展示错误。
    #
    # 检测条件：存在 browser_error 且时间窗口内有 200 OK 的业务 API 调用
    # （排除 /api/log/ 上报本身和 OPTIONS 预检）
    if browser_errors:
        for err in browser_errors:
            err_msg = str(err.get("message", ""))
            err_time = _parse_timestamp(err.get("timestamp", ""))

            # 检查是否为 undefined 访问类错误
            is_undefined_error = "undefined" in err_msg.lower() and (
                "cannot read" in err_msg.lower() or "is not a function" in err_msg.lower()
            )

            if is_undefined_error and err_time:
                # 查找时间窗口内返回 200 的业务 API
                for log in logs:
                    log_time = _parse_timestamp(log.get("timestamp", ""))
                    if not log_time:
                        continue
                    delta = abs((log_time - err_time).total_seconds())
                    if delta > 5.0:
                        continue

                    log_msg = str(log.get("message", log.get("line", "")))
                    if "200" not in log_msg:
                        continue
                    if "/api/log/" in log_msg or "OPTIONS" in log_msg:
                        continue
                    if "GET" not in log_msg and "POST" not in log_msg:
                        continue

                    anomalies.append({
                        "type": "contract_anomaly",
                        "description": (
                            f"疑似 API 契约缺陷：前端崩溃（{err_msg[:80]}），"
                            f"但附近 API 返回 200：{log_msg[:100]}"
                        ),
                        "severity": "high",
                        "browser_error": err_msg[:200],
                        "api_log": log_msg[:200],
                        "time_delta_s": delta,
                    })
                    break  # 一个错误只关联一个 API

    return anomalies
```

**改动文件**：`doctor/src/tools/observability_unified.py`

> **注意**：`_detect_anomalies` 需要接收 `browser_errors` 参数。
> 调用方需传入 `browser_errors` 列表。

#### 2.3.2 多 Trace 关联分析

**问题**：当前 auto 模式最多取 5 个 trace_id，但只是分别查 trace 数据，不做跨 trace 分析。

**方案**：增加跨 trace 关联分析——找出多个请求共享的异常模式：

```python
def _cross_trace_analysis(traces_by_id: dict[str, list[dict]]) -> dict:
    """跨 trace 关联分析。

    分析维度：
    1. 共享慢查询：多个 trace 中是否有相同的慢 SQL
    2. 共享错误端点：多个请求是否都失败在同一个 API 端点
    3. 时间聚集：错误是否集中在某个时间点（暗示触发事件）
    4. 共享依赖：多个 trace 是否都经过同一个下游服务
    """
    all_db_stmts: dict[str, list[str]] = {}
    for tid, spans in traces_by_id.items():
        stmts = [s.get("db_statement", "") for s in spans if s.get("db_statement")]
        if stmts:
            all_db_stmts[tid] = stmts

    sql_to_traces: dict[str, list[str]] = defaultdict(list)
    for tid, stmts in all_db_stmts.items():
        for sql in stmts:
            normalized = _normalize_sql(sql)
            sql_to_traces[normalized].append(tid)

    shared_sqls = {
        sql: tids for sql, tids in sql_to_traces.items()
        if len(set(tids)) >= 2
    }

    return {
        "shared_slow_queries": shared_sqls,
        "total_traces_analyzed": len(traces_by_id),
    }
```

**改动文件**：`doctor/src/tools/observability_unified.py`

#### 2.3.3 因果链重建

**问题**：当前返回的 trace 数据是扁平的 span 列表，Agent 需要自己构建因果链。

**方案**：利用已有的 `build_cross_tier_tree()`（`trace_query.py`）构建 span 树，然后提取因果链：

```python
def _build_causal_chain(tree: SpanNode) -> list[str]:
    """从 span 树中提取因果链。

    例如：
    frontend fetch /api/tasks (error)
      → backend GET /api/tasks (error)
        → DB SELECT * FROM tasks (slow, 1200ms)
        → DB SELECT * FROM comments (×50, N+1)

    输出因果链：
    ["DB N+1 comments (×50, 1200ms)", "backend /api/tasks timeout", "frontend fetch error"]
    """
    chain = []
    def _postfix(node: SpanNode, depth: int):
        for child in node.children:
            _postfix(child, depth + 1)
        if node.is_error or node.is_slow():
            prefix = "  " * depth
            label = node.name
            if node.is_db_span:
                label = f"DB: {node.db_statement[:60]}"
            chain.append(f"{prefix}[{node.service_tier}] {label} ({node.duration_ms:.0f}ms, {node.status})")

    _postfix(tree, 0)
    return chain
```

**改动文件**：`doctor/src/tools/observability_unified.py`（调用 `trace_query.py` 的 `build_cross_tier_tree`）

#### 2.3.4 静默缺陷因果链——覆盖"200 OK 但响应有问题"的场景

> **背景**：方向 2.3.3 的因果链重建仅提取 `error` 或 `slow` 节点。但企业现实中，
> 相当一部分根因是"静默的"——后端 API 返回 200 OK、无慢 span、无 ERROR 日志，
> 但响应内容缺少字段或类型不匹配，导致前端崩溃。
>
> 典型案例 FE-020：`GET /api/tasks` 返回 200（3.3ms），span 完全正常，
> 但 `TaskResponse` 不含 `tags` 字段 → 前端 `task.tags.length` → TypeError。
> 因果链重建会跳过这个 200 OK 的 span，而它恰恰是根因所在。

**方案**：增加"契约缺陷"因果链分支——当存在 browser_error 但后端无 error/slow span 时，
自动构建"前端崩溃 ← 字段缺失 ← API 200 OK ← schema 定义"的因果链：

```python
def _build_silent_defect_chain(
    browser_errors: list[dict],
    logs: list[dict],
    traces: list[dict],
    time_window_s: float = 5.0,
) -> list[str]:
    """构建静默缺陷因果链。

    当 browser_error 存在但后端无 error/slow span 时触发。
    通过错误语义 + 时间窗口关联，推断"API 200 OK 但响应有问题"的因果链。

    输出示例：
    [
        "frontend SortableTaskCard crash (TypeError: undefined 'tags')",
        "  ← task.tags 为 undefined（前端假设字段存在）",
        "    ← GET /api/projects/{id}/tasks 返回 200 (3.3ms) — 响应缺少 tags 字段",
        "      ← 疑似后端 TaskResponse schema 未定义 tags 字段",
        "         建议：用 code_search('TaskResponse') 查看 schema 定义",
    ]
    """
    import re

    UNDEFINED_PATTERN = re.compile(
        r"Cannot read properties of undefined \(reading '(\w+)'\)"
    )

    chains: list[str] = []

    for err in browser_errors:
        msg = str(err.get("message", ""))
        match = UNDEFINED_PATTERN.search(msg)
        if not match:
            continue

        missing_field = match.group(1)
        err_time = _parse_timestamp(err.get("timestamp", ""))
        if not err_time:
            continue

        # 查找时间窗口内返回 200 的业务 API
        for log in logs:
            log_time = _parse_timestamp(log.get("timestamp", ""))
            if not log_time:
                continue
            delta = abs((log_time - err_time).total_seconds())
            if delta > time_window_s:
                continue

            log_msg = str(log.get("message", log.get("line", "")))
            if "200" not in log_msg or "/api/log/" in log_msg or "OPTIONS" in log_msg:
                continue
            if "GET" not in log_msg and "POST" not in log_msg:
                continue

            # 提取 API 路径
            api_path = _extract_api_path(log_msg)

            chains.append(
                f"frontend crash (TypeError: undefined '{missing_field}')\n"
                f"  ← {missing_field} 为 undefined（前端假设字段存在）\n"
                f"    ← {log_msg[:120]} — 响应可能缺少 {missing_field} 字段\n"
                f"      ← 疑似后端 schema 未定义 {missing_field} 字段\n"
                f"         建议：用 code_search('{missing_field}') 查看字段使用\n"
                f"         建议：用 code_search('TaskResponse') 查看 schema 定义"
            )
            break

    return chains
```

**集成到 `search_observability` 返回结果**：当 `_detect_anomalies` 检测到
`contract_anomaly` 类型异常时，自动调用 `_build_silent_defect_chain()`，
将因果链追加到洞察摘要中。

**改动文件**：`doctor/src/tools/observability_unified.py`

### 2.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 异常检测层 | 高——直接给 Agent 结构化洞察 | 1.5d | P0 |
| 多 Trace 关联 | 中——当前 case 多为单 trace | 1d | P1 |
| 因果链重建 | 高——减少 Agent 推理负担 | 1d | P0 |
| **静默缺陷因果链** | **高——覆盖 200 OK 但响应有问题的跨层场景** | **1d** | **P0** |

---

## 3. code_search：从向量检索到 ripgrep 混合检索

### 3.1 现状

`doctor/src/tools/code_search.py` 当前使用 `KnowledgeService.search_code()`——基于 Qdrant 向量相似度检索。

**问题**：
1. 诊断场景中，Agent 几乎总是有具体标识符（函数名、类名、错误消息中的关键词），精确匹配远比语义搜索有效
2. 向量检索需要预先构建索引（`init_kb.py`），代码变更后需要重建
3. 向量检索返回的是"语义相似"的代码块，不一定是包含目标标识符的代码
4. 业界 SWE-bench 排名靠前的系统（SWE-agent、OpenHands、AutoCodeRover）全部使用 grep 而非向量检索

### 3.2 目标

将 `code_search` 从纯向量检索改为 **ripgrep 优先 + 向量兜底** 的混合检索。

### 3.3 具体方案

#### 3.3.1 ripgrep 后端实现

```python
# doctor/src/tools/code_search.py 重构

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)

CODEBASE_ROOT = settings.base_dir.parent / "demo-app"
MAX_RESULTS = 20
CONTEXT_LINES = 3


@traced()
async def code_search(query: str, k: int = 10) -> str:
    """
    在 demo-app 代码库中搜索代码。

    优先使用 ripgrep 精确匹配（函数名、类名、变量名、错误消息关键词）。
    如果精确匹配无结果，回退到语义向量检索。

    Args:
        query: 搜索关键词。可以是：
               - 函数名："list_tasks"
               - 类名："TaskService"
               - 错误消息片段："Cannot read properties of undefined"
               - SQL 片段："selectinload"
        k: 返回结果数量上限（默认 10，最大 20）
    """
    k = min(max(1, k), MAX_RESULTS)

    # 第一步：ripgrep 精确匹配
    rg_results = await _ripgrep_search(query, k=k)
    if rg_results:
        logger.info("code_search_ripgrep_hit", query=query[:80], count=len(rg_results))
        return json.dumps(rg_results, ensure_ascii=False)

    # 第二步：向量检索兜底
    logger.info("code_search_ripgrep_miss_fallback_vector", query=query[:80])
    vector_results = await _vector_search(query, k=k)
    return json.dumps(vector_results, ensure_ascii=False)


async def _ripgrep_search(query: str, k: int = 10) -> list[dict[str, Any]]:
    """使用 ripgrep 在代码库中搜索。"""
    safe_query = re.escape(query)

    # 尝试 whole-word 匹配
    results = await _run_ripgrep(safe_query, whole_word=True, k=k)
    if results:
        return results

    # 回退到子串匹配
    results = await _run_ripgrep(safe_query, whole_word=False, k=k)
    return results


async def _run_ripgrep(
    pattern: str,
    whole_word: bool,
    k: int,
) -> list[dict[str, Any]]:
    """执行 ripgrep 命令并解析结果。"""
    cmd = [
        "rg",
        "--json",
        "--max-count", str(k),
        "-C", str(CONTEXT_LINES),
        "--type", "py",
        "--type", "ts",
        "--type", "tsx",
        "--type", "js",
        "--type", "jsx",
    ]
    if whole_word:
        cmd.append("-w")
    cmd.extend([pattern, str(CODEBASE_ROOT)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode not in (0, 1):
            logger.error("ripgrep_error", stderr=stderr.decode()[:200])
            return []
        return _parse_ripgrep_json(stdout.decode())
    except asyncio.TimeoutError:
        logger.warning("ripgrep_timeout", pattern=pattern[:50])
        return []
    except FileNotFoundError:
        logger.warning("ripgrep_not_found_fallback_vector")
        return []


def _parse_ripgrep_json(output: str) -> list[dict[str, Any]]:
    """解析 ripgrep --json 输出为结构化结果。"""
    results = []
    current_match = None

    for line in output.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") == "match":
            match_data = data["data"]
            current_match = {
                "file_path": match_data["path"]["text"],
                "line_number": match_data["line_number"],
                "line_content": match_data["lines"]["text"].rstrip(),
                "match_type": "exact",
                "context_before": [],
                "context_after": [],
            }
            results.append(current_match)
        elif data.get("type") == "context" and current_match:
            ctx_data = data["data"]
            ctx_line = {
                "line_number": ctx_data["line_number"],
                "content": ctx_data["lines"]["text"].rstrip(),
            }
            if ctx_data["line_number"] < current_match["line_number"]:
                current_match["context_before"].append(ctx_line)
            else:
                current_match["context_after"].append(ctx_line)

    return results


async def _vector_search(query: str, k: int = 5) -> list[dict[str, Any]]:
    """向量检索兜底（保留现有 KnowledgeService.search_code）。"""
    try:
        from src.knowledge.hybrid_service import get_knowledge_service
        svc = get_knowledge_service()
        docs = await svc.search_code(query, k=k)
        return [
            {
                "file_path": doc.metadata.get("file_path", "unknown"),
                "line_number": doc.metadata.get("start_line", 0),
                "line_content": doc.page_content[:500],
                "match_type": "semantic",
                "score": doc.metadata.get("_score", 0.0),
            }
            for doc in docs
        ]
    except Exception as exc:
        logger.error("vector_search_failed", error=str(exc))
        return []
```

**改动文件**：`doctor/src/tools/code_search.py`（完全重写）

#### 3.3.2 搜索结果增强

在 ripgrep 结果中增加文件类型标注和函数/类上下文：

```python
def _enrich_result(result: dict) -> dict:
    """为搜索结果增加上下文信息。"""
    path = result["file_path"]
    if "/api/" in path:
        result["file_role"] = "api_route"
    elif "/services/" in path:
        result["file_role"] = "business_logic"
    elif "/models/" in path:
        result["file_role"] = "data_model"
    elif "/schemas/" in path:
        result["file_role"] = "api_schema"
    elif "/pages/" in path:
        result["file_role"] = "frontend_page"
    elif "/components/" in path:
        result["file_role"] = "frontend_component"
    else:
        result["file_role"] = "other"

    result["relative_path"] = str(Path(path).relative_to(CODEBASE_ROOT))
    return result
```

### 3.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| ripgrep 后端 | 极高——诊断准确率直接提升 | 1d | P0 |
| 结果增强 | 中——改善 Agent 理解 | 0.5d | P1 |
| 向量兜底保留 | 低——大部分场景 ripgrep 够用 | 0d | P2 |

---

## 4. 上下文工程：压缩、预算、动态策略

> **Agent 推理质量的地基。** 依赖方向 0（手动循环）提供注入点。

### 4.1 现状

当前代码暴露了 4 个严重问题：

1. **工具结果零管控**：`search_observability` 可能返回数百行日志/Trace span，`get_file_content` 可能返回整个文件，`db_query` 可能返回大结果集——这些结果原样累积在 messages 列表中，无任何压缩、摘要或预算控制。

2. **硬截断而非优雅降级**：`MAX_TOOL_CALLS=12` 是粗暴的截断——到 12 次直接停。`MAX_TOKENS_BUDGET=100_000` 是软上限但**没有触发任何压缩动作**。

3. **System Prompt 静态化**：`_build_system_prompt()` 在 Agent 构建时渲染一次，模块级缓存。不会根据诊断进展动态调整。

4. **模型推理质量退化**：LLM 在 context 使用超过 60% 容量后，推理质量显著下降（"lost in the middle"效应）。当前 12 次调用的 token 数学：
   ```
   System Prompt:     ~2,000 tokens
   Evidence Message:  ~1,500 tokens
   12 × 工具结果:     ~36,000 tokens（保守估计，实际可能 60k+）
   12 × Agent 推理:   ~6,000 tokens
   ────────────────────────────────
   总计:              ~45,500 tokens（复杂 case 轻松突破 80k）
   ```

### 4.2 目标

实现**四层压缩 + 动态策略注入**，让 Agent 在整个诊断过程中保持推理质量。

### 4.3 具体方案

```python
# doctor/src/graph/context_engine.py 新建

"""
上下文引擎 — 四层压缩 + 动态策略注入。

设计原则：
1. 工具结果不原样保留——进入 context 前先过预算检查
2. 历史消息不无限累积——早期工具结果逐步降级为摘要
3. System Prompt 不静态不变——根据诊断进展动态注入策略
4. 接近预算上限时——切换为"快速结论"模式，不再调工具
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


class ContextPhase(str, Enum):
    """诊断阶段，决定 System Prompt 策略。"""
    INITIAL = "initial"        # 初始：完整策略，鼓励探索
    INVESTIGATING = "investigating"  # 调查中：根据信号类型注入专项策略
    CONVERGING = "converging"  # 收敛中：减少探索，鼓励验证
    FINALIZING = "finalizing"  # 终结中：必须输出结论，不再调工具


@dataclass
class ContextBudget:
    """上下文预算追踪器。"""
    model_context_window: int = 128_000
    reserved_for_output: int = 4_000
    warning_threshold: float = 0.6       # 60% 开始降级
    critical_threshold: float = 0.8      # 80% 强制终结

    system_prompt_tokens: int = 0
    evidence_tokens: int = 0
    tool_result_tokens: int = 0
    agent_reasoning_tokens: int = 0

    @property
    def total_used(self) -> int:
        return (
            self.system_prompt_tokens
            + self.evidence_tokens
            + self.tool_result_tokens
            + self.agent_reasoning_tokens
        )

    @property
    def available(self) -> int:
        return self.model_context_window - self.reserved_for_output - self.total_used

    @property
    def usage_ratio(self) -> float:
        denom = self.model_context_window - self.reserved_for_output
        return self.total_used / denom if denom > 0 else 1.0

    @property
    def phase(self) -> ContextPhase:
        r = self.usage_ratio
        if r >= self.critical_threshold:
            return ContextPhase.FINALIZING
        elif r >= self.warning_threshold:
            return ContextPhase.CONVERGING
        elif self.tool_result_tokens > 0:
            return ContextPhase.INVESTIGATING
        else:
            return ContextPhase.INITIAL


# ═════════════════════════════════════════════════════════════════════
# Layer 1: 工具结果预算控制（入 context 前截断）
# ═════════════════════════════════════════════════════════════════════

TOOL_RESULT_BUDGET = {
    "search_observability": 1500,
    "code_search": 1000,
    "get_file_content": 2000,
    "db_query": 800,
    "inspect_frontend_error": 1000,
}


def truncate_tool_result(tool_name: str, content: str) -> str:
    """工具结果入 context 前的预算控制。

    策略：
    - 超预算时保留关键信息（错误、异常、trace_id、行号）
    - 头尾保留，中间摘要
    - 追加 [已压缩] 标记
    """
    budget = TOOL_RESULT_BUDGET.get(tool_name, 1500)
    estimated_tokens = len(content) // 4

    if estimated_tokens <= budget:
        return content

    key_keywords = [
        "error", "exception", "trace", "span", "fail",
        "line", "raise", "traceback", "status_code",
        "null", "undefined", "timeout",
    ]

    lines = content.split("\n")
    key_lines = [
        (i, line) for i, line in enumerate(lines)
        if any(kw in line.lower() for kw in key_keywords)
    ]

    if len(key_lines) >= 10:
        kept = [f"  L{idx+1}: {line}" for idx, line in key_lines[:40]]
        return (
            f"[工具结果已压缩：{estimated_tokens}→~{len(kept)*15} tokens，"
            f"保留 {len(kept)} 行关键信息]\n"
            + "\n".join(kept)
        )
    else:
        head = lines[:15]
        tail = lines[-10:]
        skipped = len(lines) - 25
        return (
            f"[工具结果已压缩：{estimated_tokens}→~{600} tokens]\n"
            + "\n".join(head)
            + f"\n  ... ({skipped} 行已省略) ...\n"
            + "\n".join(tail)
        )


# ═════════════════════════════════════════════════════════════════════
# Layer 2: 历史消息降级（早期工具结果逐步摘要）
# ═════════════════════════════════════════════════════════════════════

def degrade_old_tool_results(messages: list[BaseMessage], keep_recent: int = 4) -> list[BaseMessage]:
    """将早期工具结果降级为一行摘要。

    保留最近 keep_recent 条工具结果原文，
    更早的替换为摘要行。
    """
    tool_indices = [
        i for i, msg in enumerate(messages)
        if isinstance(msg, ToolMessage)
    ]

    if len(tool_indices) <= keep_recent:
        return messages

    to_degrade = tool_indices[:-keep_recent]
    to_archive = tool_indices[:-keep_recent * 2] if len(tool_indices) > keep_recent * 2 else []

    result = list(messages)
    for idx in to_degrade:
        if idx in to_archive:
            tool_name = getattr(result[idx], "name", "unknown")
            result[idx] = ToolMessage(
                content=f"[已归档：工具 {tool_name} 的结果已省略]",
                tool_call_id=getattr(result[idx], "tool_call_id", ""),
                name=tool_name,
            )
        else:
            content = str(result[idx].content)
            first_line = content.split("\n")[0][:200]
            result[idx] = ToolMessage(
                content=f"[已摘要] {first_line}...",
                tool_call_id=getattr(result[idx], "tool_call_id", ""),
                name=getattr(result[idx], "name", "unknown"),
            )

    return result


# ═════════════════════════════════════════════════════════════════════
# Layer 3: 动态 System Prompt 注入（根据阶段切换策略）
# ═════════════════════════════════════════════════════════════════════

PHASE_STRATEGIES = {
    ContextPhase.INITIAL: """
## 当前阶段：初始探索

你有充足的预算。请系统性地调查：
1. 先调 search_observability 获取全貌
2. 根据信号类型选择后续工具
3. 不要急于下结论，收集足够证据
""",

    ContextPhase.INVESTIGATING: """
## 当前阶段：深入调查

你已获取初步数据。请聚焦：
1. 针对最可疑的信号深入
2. 用 code_search / get_file_content 定位代码
3. 用 db_query 验证数据状态
4. 每次工具调用前明确"我要验证什么假设"
""",

    ContextPhase.CONVERGING: """
## 当前阶段：收敛

⚠️ 上下文预算已使用 {ratio:.0%}，请开始收敛：
1. 不再探索新方向，聚焦当前最强假设
2. 最多再调 2-3 次工具做验证
3. 如果当前证据已足够，直接输出结论
""",

    ContextPhase.FINALIZING: """
## 当前阶段：强制终结

🚨 上下文预算已使用 {ratio:.0%}，必须立即输出结论：
1. 不要再调用任何工具
2. 基于当前已有证据输出最佳诊断报告
3. 在 notes 中说明"因预算限制提前终止"
4. confidence 不超过 0.6（证据可能不完整）
""",
}


def build_dynamic_system_prompt(
    base_prompt: str,
    budget: ContextBudget,
    diagnosis_hints: dict[str, Any] | None = None,
) -> str:
    """根据预算阶段动态组装 System Prompt。"""
    phase = budget.phase
    strategy = PHASE_STRATEGIES[phase].format(ratio=budget.usage_ratio)

    parts = [base_prompt, strategy]

    if diagnosis_hints:
        hints_text = _format_diagnosis_hints(diagnosis_hints)
        if hints_text:
            parts.append(f"## 诊断进展\n{hints_text}")

    parts.append(
        f"## 预算状态\n"
        f"- 已用 tokens: ~{budget.total_used:,}\n"
        f"- 剩余 tokens: ~{budget.available:,}\n"
        f"- 工具结果占用: ~{budget.tool_result_tokens:,}\n"
        f"- 阶段: {phase.value}"
    )

    return "\n\n---\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
# Layer 4: 自动压缩触发器（集成到 Agent 循环）
# ═════════════════════════════════════════════════════════════════════

async def maybe_compact_context(
    messages: list[BaseMessage],
    budget: ContextBudget,
) -> tuple[list[BaseMessage], bool]:
    """检查是否需要压缩，如需要则执行。"""
    compacted = False

    if budget.usage_ratio > budget.warning_threshold:
        messages = degrade_old_tool_results(messages, keep_recent=3)
        compacted = True

    if budget.usage_ratio > 0.75:
        messages = _aggressive_compact(messages)
        compacted = True

    return messages, compacted
```

**集成到 Agent 循环**：在方向 0 的手动循环中，每个迭代开始时调用 `maybe_compact_context`，工具结果入 messages 前调用 `truncate_tool_result`。

**改动文件**：
- `doctor/src/graph/context_engine.py`（新建，~300 行）
- `doctor/src/graph/nodes/unified_agent.py`（修改，集成到手动循环）

### 4.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 工具结果截断（Layer 1） | 极高——直接解决 context 膨胀 | 1d | P0 |
| 历史消息降级（Layer 2） | 高——长诊断场景必需 | 1d | P0 |
| 动态策略注入（Layer 3） | 中——依赖方向 6 先完成 | 0.5d | P1 |
| 自动压缩触发（Layer 4） | 高——串联 1+2 | 0.5d | P0 |

---

## 5. 评测体系：从"打分"到"诊断质量门禁"

### 5.1 现状

`benchmark/src/benchmark/` 已实现：

- **Runner**（`runner.py`）：加载 case → 调用 Doctor → 收集结果
- **Evaluators**（`evaluators/`）：
  - `exact_match.py`：精确匹配（affected_file、root_cause 关键词）
  - `keyword_match.py`：关键词匹配（fix_keywords 命中率）
  - `llm_judge.py`：LLM 评分（GPT-4o 作为 Judge，返回 0~1 分）
  - `efficiency.py`：效率评估（工具调用次数、token 消耗、耗时）
- **Reporters**：JSON / Markdown 报告

**不足**：
1. 评测维度耦合——root_cause 正确但 affected_file 错误时，exact_match 直接 0 分
2. 没有"盲集"隔离——所有 case 都在 gold 目录，开发时可能过拟合
3. 没有回归门禁——无法说"这次改动让准确率下降了 2σ，必须回滚"
4. 评测只看最终结果，不看诊断过程（工具调用序列是否合理）

### 5.2 目标

建立**多维度解耦评测 + 回归门禁**体系，让每次代码变更都有可量化的质量评估。

### 5.3 具体方案

#### 5.3.1 评测维度解耦

```python
# benchmark/schema.py 新增

class DimensionScores(BaseModel):
    """多维度解耦评分。"""
    root_cause_accuracy: float = Field(ge=0, le=1, description="根因定位准确度")
    affected_file_accuracy: float = Field(ge=0, le=1, description="文件定位准确度")
    affected_line_accuracy: float = Field(ge=0, le=1, description="行号定位准确度")
    fix_suggestion_quality: float = Field(ge=0, le=1, description="修复建议质量")
    category_accuracy: float = Field(ge=0, le=1, description="Bug 类别分类准确度")
    cross_layer_identification: float = Field(
        ge=0, le=1,
        description=(
            "跨层识别能力：是否正确区分症状层和根因层。"
            "对于跨层 bug（如前端崩溃但根因在后端），"
            "仅诊断症状层 = 0.3，同时诊断症状和根因 = 1.0。"
            "对于单层 bug，此项默认 1.0。"
        ),
    )
    evidence_chain_completeness: float = Field(ge=0, le=1, description="证据链完整性")
    confidence_calibration: float = Field(ge=0, le=1, description="置信度校准度")

    @property
    def overall(self) -> float:
        weights = {
            "root_cause_accuracy": 0.25,
            "affected_file_accuracy": 0.15,
            "affected_line_accuracy": 0.10,
            "fix_suggestion_quality": 0.15,
            "category_accuracy": 0.10,
            "cross_layer_identification": 0.10,
            "evidence_chain_completeness": 0.10,
            "confidence_calibration": 0.05,
        }
        return sum(getattr(self, k) * w for k, w in weights.items())
```

**改动文件**：`benchmark/src/benchmark/schema.py`、`benchmark/src/benchmark/evaluators/`

#### 5.3.2 过程质量评估

```python
# benchmark/src/benchmark/evaluators/process.py 新增

class ProcessEvaluator(BaseEvaluator):
    """评估诊断过程的工具调用质量。

    评估维度：
    1. 工具选择合理性：是否调用了正确的工具
    2. 工具调用效率：是否有冗余调用
    3. 调用顺序合理性：是否先 search_observability 再 code_search
    4. 预算使用率：工具调用次数 / MAX_TOOL_CALLS
    5. 证据覆盖度：是否查看了 affected_file
    """

    name = "process"

    async def evaluate(self, case, result):
        tool_calls = result.tool_call_trace or []
        scores = []

        # 工具选择合理性
        case_category = case.expected.root_cause.lower()
        if "frontend" in case_category or "前端" in case_category:
            has_frontend_tool = any(
                "frontend" in str(tc.get("tool", "")) for tc in tool_calls
            )
            scores.append(1.0 if has_frontend_tool else 0.5)

        # 调用顺序
        first_observability = next(
            (i for i, tc in enumerate(tool_calls)
             if "observability" in str(tc.get("tool", ""))),
            None
        )
        if first_observability is not None:
            scores.append(1.0 if first_observability <= 2 else 0.7)
        else:
            scores.append(0.3)

        # 预算使用率
        budget_ratio = len(tool_calls) / 12
        if budget_ratio <= 0.5:
            scores.append(1.0)
        elif budget_ratio <= 0.8:
            scores.append(0.7)
        else:
            scores.append(0.4)

        avg_score = sum(scores) / len(scores) if scores else 0.5
        return EvaluationScore(
            evaluator=self.name,
            score=avg_score,
            reasoning=f"工具调用 {len(tool_calls)} 次，过程评分 {avg_score:.2f}",
        )
```

**改动文件**：`benchmark/src/benchmark/evaluators/process.py`（新建）

#### 5.3.3 回归门禁

```python
# benchmark/src/benchmark/regression.py 新增

class RegressionGate:
    """回归门禁：与基线对比，检测性能退化。"""

    def __init__(self, baseline_path: str):
        self.baseline = self._load_baseline(baseline_path)

    def check(self, current_scores: list[DimensionScores]) -> RegressionResult:
        current_mean = statistics.mean([s.overall for s in current_scores])
        baseline_mean = self.baseline["overall_mean"]
        baseline_std = self.baseline["overall_std"]

        delta = current_mean - baseline_mean
        delta_sigma = delta / baseline_std if baseline_std > 0 else 0

        if delta_sigma < -2:
            return RegressionResult(status="BLOCK", message=f"综合分下降 {abs(delta_sigma):.1f}σ")
        elif delta_sigma > 1:
            return RegressionResult(status="IMPROVE", message=f"综合分提升 {delta_sigma:.1f}σ")
        return RegressionResult(status="PASS", message="无显著变化")
```

**改动文件**：`benchmark/src/benchmark/regression.py`（新建）

#### 5.3.4 盲集隔离

将 gold 目录的 case 分为 train/test 两部分：

```
bug-factory/recipes/gold/
├── train/    # 开发时可查看、调试（10 个）
└── blind/    # 仅评测时使用，开发时不查看（5 个）
```

**改动文件**：`bug-factory/recipes/gold/` 目录结构调整、`benchmark/src/benchmark/loader.py`

### 5.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 评测维度解耦 | 高——精确定位退化点 | 1.5d | P1 |
| 过程质量评估 | 中——过程好结果才稳定 | 1d | P1 |
| 回归门禁 | 高——CI/CD 质量保障 | 1d | P1 |
| 盲集隔离 | 高——防止过拟合 | 0.5d | P1 |

---

## 6. System Prompt 工程：从"指令"到"策略"

### 6.1 现状

`doctor/src/prompts/templates/unified_agent.j2` 当前是一个静态模板，包含：
- 诊断三步骤（理解证据 → 深入调查 → 定位根因）
- 工具选择表
- 输出 JSON 格式定义
- 预算约束

**不足**：
1. 没有 few-shot 示例——Agent 不知道"好的诊断报告"长什么样
2. 策略是静态的——不管什么类型的 Bug 都走同一套流程
3. 没有错误模式库——常见错误模式没有预设诊断路径

### 6.2 目标

让 System Prompt 从"固定指令"升级为"动态策略"——根据证据类型选择不同的诊断路径和 few-shot 示例。

### 6.3 具体方案

#### 6.3.1 动态策略选择

```python
# doctor/src/graph/nodes/unified_agent.py 修改

def _select_strategy(evidence: NormalizedEvidence) -> str:
    """根据证据类型选择诊断策略。"""
    signals = evidence.golden_signals
    has_error_span = any(s.signal_type == "error_span" for s in signals)
    has_slow_span = any(s.signal_type == "slow_span" for s in signals)
    has_repeated_query = any(s.signal_type == "repeated_query" for s in signals)
    has_browser_error = any(s.source == "browser_error" for s in signals)
    has_smokeless = any(s.signal_type.startswith("smokeless") for s in signals)

    # 跨层关联检测：不依赖 trace_id，使用多级关联结果
    # （方向 1.3.5 的 correlate_multi_level 已填充 correlations）
    has_cross_layer = bool(evidence.correlations)

    # 兜底：即使 correlations 为空，如果前端崩溃且后端有 200 OK 的 API 调用，
    # 也应怀疑跨层问题（企业现实中 trace_id 常不匹配）
    if not has_cross_layer and has_browser_error:
        has_cross_layer = _infer_cross_layer_from_signals(signals, evidence)

    if has_cross_layer and has_browser_error:
        return "cross_layer_crash"
    elif has_repeated_query or has_slow_span:
        return "performance"
    elif has_error_span:
        return "backend_error"
    elif has_browser_error:
        return "frontend_crash"
    elif has_smokeless:
        return "smokeless"
    else:
        return "default"


def _infer_cross_layer_from_signals(
    signals: list[Signal],
    evidence: NormalizedEvidence,
) -> bool:
    """当 correlations 为空时的跨层推断兜底。

    企业现实中，前端崩溃和后端 API 的 trace_id 常不匹配，
    correlator 可能无法生成 Correlation。此时通过信号特征推断：
    - 前端有 browser_error（TypeError: undefined）
    - 后端有 200 OK 的 API 调用（非 /api/log/ 上报）
    → 疑似跨层 API 契约缺陷
    """
    has_frontend_crash = any(
        s.service_tier == "frontend" and "undefined" in s.summary.lower()
        for s in signals
    )
    # 检查 raw_refs 中是否有 200 OK 的业务 API 调用
    raw_logs = evidence.raw_refs.get("logs", [])
    has_normal_api = any(
        "200" in str(log.get("line", "")) and "/api/log/" not in str(log.get("line", ""))
        for log in raw_logs
    )
    return has_frontend_crash and has_normal_api


_CROSS_LAYER_STRATEGY = """
### 跨层崩溃诊断策略

当前证据显示前端崩溃且存在跨层关联。诊断路径：

1. 用 inspect_frontend_error 分析浏览器错误，提取错误类型和栈帧
2. 用 source_map_resolve 定位到源码文件和行号
3. 用 code_search 搜索报错变量/字段的来源
4. 用 search_observability 查看对应 API 请求的响应内容
5. 用 get_file_content 查看后端 schema 定义，确认字段是否缺失

常见模式：
- TypeError: Cannot read properties of undefined → 后端 API 未返回该字段
- TypeError: x is not a function → 前端导入错误或 API 返回类型不符
"""

_PERFORMANCE_STRATEGY = """
### 性能问题诊断策略

当前证据显示性能问题（慢 span / N+1 查询）。诊断路径：

1. 用 search_observability(source="tempo") 查看完整 trace
2. 找到耗时最长的 span，确认是 DB 查询还是业务逻辑
3. 如果是 DB 查询：
   a. 用 code_search 搜索对应的 ORM 查询代码
   b. 检查是否缺少 selectinload/joinedload 预加载
   c. 检查是否有 N+1 循环查询模式
4. 如果是业务逻辑：
   a. 用 get_file_content 查看对应函数实现
   b. 检查是否有同步阻塞操作

常见模式：
- N+1 查询：循环内单独查询关联数据 → 恢复预加载
- 缺少索引：查询全表扫描 → 添加数据库索引
"""

_SMOKELESS_STRATEGY = """
### 无信号诊断策略

当前证据无错误信号（日志/Trace 全部正常）。这类 Bug 通常是逻辑/数据/配置问题。

诊断路径：
1. 仔细分析 user_report，提取关键词（"越权"、"排序不对"、"数据丢失"）
2. 用 code_search 搜索相关 API 端点
3. 用 get_file_content 查看端点实现，检查：
   a. 权限过滤：是否有 owner_id / user_id 过滤？
   b. 数据排序：ORDER BY 是否正确？
   c. 字段映射：Pydantic schema 是否遗漏字段？
   d. 配置项：JWT 过期时间、分页大小等是否正确？
4. 用 db_query 验证数据库中的实际数据状态

常见模式：
- IDOR 越权：缺少 owner_id 过滤 → 恢复权限检查
- 排序错误：ORDER BY 字段错误或缺失 → 修正排序逻辑
- 字段丢失：Schema 遗漏字段 → 补齐 schema 定义
"""
```

**改动文件**：`doctor/src/graph/nodes/unified_agent.py`、`doctor/src/prompts/templates/`（新增策略片段）

#### 6.3.2 Few-shot 示例注入

```python
# doctor/src/prompts/templates/few_shot/ 新建

FEW_SHOT_EXAMPLES = {
    "cross_layer_crash": """
## 诊断示例

**输入证据**：
- 浏览器错误：TypeError: Cannot read properties of undefined (reading 'length')
- 栈帧：TaskBoardPage.tsx:148
- 跨层关联：前端崩溃 → 后端 GET /api/tasks（注意：trace_id 可能不匹配，
  企业现实中前端渲染崩溃与后端 API 调用通常使用不同 trace_id）

**诊断过程**：
1. inspect_frontend_error → 错误类型：undefined_access，定位到 TaskBoardPage.tsx:148
2. get_file_content(TaskBoardPage.tsx, 140-155) → 发现 `task.tags.length` 未判空
3. code_search("TaskResponse") → 找到后端 schema 定义
4. get_file_content(schemas/task.py) → TaskResponse 未包含 tags 字段
5. search_observability → 确认 API 响应中无 tags 字段（即使 trace_id 不匹配，
   也可通过时间窗口内的 200 OK API 调用关联）

**输出**：
{
  "primary_category": "frontend_crash",
  "root_cause": "后端 TaskResponse schema 未返回 tags 字段，前端直接读取 task.tags.length 导致 TypeError",
  "affected_file": "demo-app/backend/app/schemas/task.py",
  "fix_suggestion": "【文件】schemas/task.py\\n【改后】在 TaskResponse 中添加 tags: list[TagResponse] 字段",
  "confidence": 0.90
}
""",
    "performance": """
## 诊断示例

**输入证据**：
- 慢 span：GET /api/projects/{id}/tasks (3200ms)
- N+1 信号：SELECT * FROM comments WHERE task_id = ? 重复 50 次

**诊断过程**：
1. search_observability(trace_id=xyz) → 确认 50 次重复 DB 查询
2. code_search("list_tasks") → 找到 tasks.py 中的端点
3. get_file_content(tasks.py, 35-50) → 发现循环内单独查询 comments
4. 确认缺少 selectinload(Task.comments)

**输出**：
{
  "primary_category": "performance",
  "root_cause": "list_tasks 删除了 selectinload(Task.comments) 预加载，改为循环内逐条查询 comments，构成 N+1",
  "affected_file": "demo-app/backend/app/api/tasks.py",
  "fix_suggestion": "【文件】tasks.py\\n【改后】恢复 .options(selectinload(Task.comments))",
  "confidence": 0.95
}
""",
}
```

**改动文件**：`doctor/src/prompts/templates/few_shot/`（新建目录）

### 6.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 动态策略选择 | 高——不同 Bug 类型走不同路径 | 1.5d | P1 |
| Few-shot 示例 | 高——Agent 学习"好诊断"长什么样 | 1d | P1 |
| 错误模式库集成 | 中——当前 15 个模式够用 | 0.5d | P2 |

---

## 7. 诊断计划（TodoWrite）

> 对应 learn-claude-code s05：先列出步骤再开始执行，完成率翻倍。

### 7.1 现状

当前 Agent 直接开始调工具，没有显式的诊断计划。容易在工具调用中"走偏"——查了 5 个工具后发现方向错了，但预算已经消耗大半。

### 7.2 目标

Agent 在第一次工具调用前输出诊断计划，每步完成后更新状态。

### 7.3 具体方案

```python
# doctor/src/prompts/templates/unified_agent.j2 追加

DIAGNOSIS_PLAN_INSTRUCTION = """
## 诊断计划

在开始任何工具调用之前，你必须先输出一个诊断计划：

<diagnosis_plan>
1. [步骤描述] — 预期工具: [工具名]
2. [步骤描述] — 预期工具: [工具名]
3. [步骤描述] — 预期工具: [工具名]
</diagnosis_plan>

每完成一个步骤后，更新计划状态：
- ✅ 已完成
- 🔄 进行中
- ⬜ 待执行

如果执行中发现计划需要调整，显式说明原因并更新计划。
"""
```

**与方向 10（假设追踪）的区别**：
- 方向 10 追踪的是"假设"（H1: N+1 查询 | 证据: ... | 置信度: ...）
- TodoWrite 追踪的是"步骤"（[x] 查日志 → [x] 查代码 → [ ] 查数据库）
- 两者互补：TodoWrite 管执行流程，假设追踪管推理质量

### 7.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 诊断计划 Prompt | 高——防止漂移 | 0.5d | P1 |
| 计划状态解析 | 中——评测可追踪 | 0.5d | P1 |

---

## 8. Bug Factory：从"静态配方"到"变异生成"

### 8.1 现状

`bug-factory/recipes/gold/` 有 15 个 YAML 配方，覆盖 7 个类别。

**不足**：
1. Bug 类型覆盖面有限——缺少 SQL 注入、XSS、CSRF 等安全类 Bug
2. 每个 Bug 是独立配方，没有"变异"能力
3. 没有"对抗性" Bug——专门设计让 Agent 容易误诊的 Bug

### 8.2 目标

从 15 个手工配方扩展到 **100+ 自动生成的多样化 Bug 场景**。

### 8.3 具体方案

#### 8.3.1 Bug 变异引擎

```python
# bug-factory/src/bug_factory/mutator.py 新建

class BugMutator:
    """从一个 Bug 配方生成多个变体。

    变异维度：
    1. 函数名/变量名替换（list_tasks → get_tasks）
    2. 字段名替换（tags → labels, comments → notes）
    3. 错误位置迁移（同一 Bug 注入到不同文件/函数）
    4. 错误程度调节（N+1 的重复次数 5→50→500）
    5. 噪声注入（在 Bug 周围添加干扰日志，测试 Agent 抗噪能力）
    """

    def mutate(self, recipe: dict, count: int = 5) -> list[dict]:
        variants = []
        for i in range(count):
            variant = self._mutate_one(recipe, seed=i)
            variants.append(variant)
        return variants
```

**改动文件**：`bug-factory/src/bug_factory/mutator.py`（新建）

#### 8.3.2 新增 Bug 类型

| 新增类别 | Bug 示例 | 难度 |
|---------|---------|------|
| security_sql_injection | API 端点直接拼接 SQL，未参数化 | L3 |
| security_xss | 前端渲染用户输入未转义 | L2 |
| security_csrf | 删除操作缺少 CSRF token 验证 | L3 |
| error_handling | except Exception: pass 吞掉异常 | L2 |
| resource_leak | 数据库连接未关闭 | L3 |
| concurrency_deadlock | 两个事务互相等待 | L4 |
| data_migration | Alembic 迁移遗漏字段 | L3 |
| api_contract | 前后端字段类型不一致 | L3 |
| caching_stale | Redis 缓存未失效，返回旧数据 | L3 |
| env_config | 环境变量名拼写错误导致使用默认值 | L2 |

**改动文件**：`bug-factory/recipes/gold/`（新增 10+ 配方文件）

#### 8.3.3 对抗性 Bug 设计

| 对抗策略 | Bug 示例 | Agent 容易犯的错误 |
|---------|---------|-------------------|
| 烟雾弹 | 日志有 ERROR 但根因是数据问题 | Agent 停留在 ERROR 日志，不深入查数据 |
| 误导性栈帧 | 前端报错指向组件 A，但根因在组件 B 的 props 传递 | Agent 只看组件 A |
| 多根因竞争 | 同时有 N+1 和越权，但用户只报告慢 | Agent 只诊断 N+1，遗漏越权 |
| 假阳性信号 | health check 偶尔超时，但不是真正的问题 | Agent 浪费预算调查 health check |

**改动文件**：`bug-factory/recipes/gold/`（新增对抗性配方）

### 8.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| Bug 变异引擎 | 高——快速扩展 case 数量 | 2d | P1 |
| 新增 Bug 类型 | 高——覆盖面 | 3d | P1 |
| 对抗性 Bug | 高——测试 Agent 鲁棒性 | 2d | P2 |

---

## 9. 安全沙箱：从"单点防护"到"纵深防御"

### 9.1 现状

`doctor/src/security/` 已实现三层防护：SQL 守卫、路径沙箱、密钥保护。

**不足**：
1. SQL 守卫只做语法层检查，不限制查询复杂度
2. 路径沙箱不限制文件类型（如 Agent 可以读取 `.env` 文件）
3. 没有工具输出脱敏——工具返回的数据可能包含密钥、Token

### 9.2 目标

建立**多层纵深防御**。

### 9.3 具体方案

#### 9.3.1 SQL 复杂度限制

```python
# doctor/src/security/sql_guard.py 新增

class SQLComplexityChecker:
    """检查 SQL 查询复杂度，防止资源耗尽。"""

    MAX_RESULT_ROWS = 1000
    MAX_TABLE_JOINS = 3

    def check(self, sql: str) -> None:
        parsed = sqlparse.parse(sql)[0]
        if not self._has_limit(parsed):
            sql = f"{sql.rstrip(';')} LIMIT {self.MAX_RESULT_ROWS}"
        join_count = self._count_joins(parsed)
        if join_count > self.MAX_TABLE_JOINS:
            raise UnsafeSQLError(f"Too many JOINs: {join_count}")
```

#### 9.3.2 文件访问白名单

```python
# doctor/src/security/sanitizer.py 新增

_FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.env", re.IGNORECASE),
    re.compile(r"credentials", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"id_rsa", re.IGNORECASE),
]

def check_file_access(path: Path) -> None:
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(str(path)):
            raise PathSandboxError(f"Access denied: {pattern.pattern}")
```

#### 9.3.3 工具输出脱敏

```python
# doctor/src/security/output_sanitizer.py 新建

class OutputSanitizer:
    """对工具返回的数据进行脱敏处理。"""

    PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
        (re.compile(r"postgresql://[^:]+:([^@]+)@"), r"postgresql://***:\1@"),
    ]

    def sanitize(self, text: str) -> str:
        for pattern, replacement in self.PATTERNS:
            text = pattern.sub(replacement, text)
        return text
```

### 9.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| SQL 复杂度限制 | 高——防止 Agent 拖垮数据库 | 0.5d | P2 |
| 文件访问白名单 | 高——防止密钥泄露 | 0.5d | P2 |
| 输出脱敏 | 中——当前 case 无敏感数据 | 1d | P2 |

---

## 10. Agent 自省与纠错机制

### 10.1 现状

当前 V3 Agent 是单轮 ReAct 循环，没有自我检查和纠错机制。

**问题**：
1. Agent 可能在早期形成错误假设，后续所有工具调用都在"确认偏误"下进行
2. Agent 不会质疑自己的结论——如果 code_search 没找到结果，Agent 可能直接放弃
3. 没有不确定性传播——Agent 的 confidence 是主观估计，不是基于证据覆盖度计算

### 10.2 目标

通过**轻量级自省机制**提升诊断可靠性。

### 10.3 具体方案

#### 10.3.1 假设追踪与证伪

```python
HYPOTHESIS_TRACKING_INSTRUCTION = """
## 假设追踪

在诊断过程中，你必须：

1. **显式记录假设**：每次基于工具结果形成假设时，用以下格式记录：
   - 假设 H1: [假设内容] | 证据: [支持证据] | 置信度: [0-1]

2. **主动证伪**：对每个假设，至少尝试一个"证伪查询"：
   - 如果假设是"N+1 查询"，用 db_query 查看实际查询日志确认
   - 如果假设是"字段缺失"，用 search_observability 查看 API 实际响应

3. **假设淘汰**：如果证伪查询的结果与假设矛盾，必须放弃该假设。
   不要忽略矛盾证据。

4. **最终选择**：从存活的假设中选择置信度最高的作为根因。
"""
```

**改动文件**：`doctor/src/prompts/templates/unified_agent.j2`

#### 10.3.2 证据覆盖度检查

```python
def _check_evidence_coverage(
    report: DiagnosisReport,
    tool_calls: list[dict],
    evidence: NormalizedEvidence,
) -> dict[str, Any]:
    """检查诊断报告的证据覆盖度，用于调整 confidence。"""
    coverage = {
        "has_observability_data": any(
            "observability" in str(tc.get("tool", "")) for tc in tool_calls
        ),
        "has_code_inspection": any(
            tc.get("tool") in ("code_search", "get_file_content") for tc in tool_calls
        ),
        "has_data_verification": any(
            "db_query" in str(tc.get("tool", "")) for tc in tool_calls
        ),
        "has_frontend_inspection": any(
            "frontend" in str(tc.get("tool", "")) for tc in tool_calls
        ),
    }

    score = 0.0
    if coverage["has_observability_data"]:
        score += 0.3
    if coverage["has_code_inspection"]:
        score += 0.3
    if coverage["has_data_verification"]:
        score += 0.2
    if coverage["has_frontend_inspection"] and report.symptom_tier == "frontend":
        score += 0.2

    coverage["coverage_score"] = score
    coverage["calibrated_confidence"] = min(report.confidence * score / 0.6, 1.0)
    return coverage
```

**改动文件**：`doctor/src/graph/nodes/unified_agent.py`

#### 10.3.3 自我审查提示

```python
SELF_REVIEW_INSTRUCTION = """
## 输出前自检

在输出最终诊断报告前，请回答以下问题：

1. **根因是否经过验证？** 你是否用工具确认了根因，还是仅基于推理？
2. **affected_file 是否准确？** 你是否用 get_file_content 查看了该文件？
3. **fix_suggestion 是否可执行？** 你是否确认了修改位置和修改内容？
4. **是否有遗漏？** 是否有未解释的信号或证据？
5. **置信度是否合理？** 0.9+ 需要工具验证 + 证据链完整。
6. **症状 vs 根因是否区分清楚？** 如果前端崩溃但根因在后端（如 schema 缺字段），
   你的 primary_category 应反映症状（如 frontend_crash），但 root_cause 和
   affected_file 必须指向后端。仅修复前端（如加可选链 `?.`）而不修复后端
   schema = 治标不治本，confidence 应降至 0.4 以下。
7. **是否检查了"静默"的后端问题？** 如果前端报 TypeError: undefined，
   附近有 200 OK 的 API 调用，你是否检查了 API 响应是否缺少字段？
   不要因为 API 返回 200 就排除后端问题。

如果以上任何一项不满足，降低 confidence 或继续调查。
"""
```

### 10.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 假设追踪与证伪 | 高——减少确认偏误 | 0.5d | P2 |
| 证据覆盖度检查 | 高——confidence 校准 | 1d | P2 |
| 自我审查提示 | 中——轻量级改进 | 0.5d | P2 |

---

## 11. 成本优化与模型路由

### 11.1 现状

`doctor/src/llm_factory.py` 已实现分层模型策略，但 V3 架构下只有 `diagnosis` 角色在使用，实际上只有一个模型在跑。

**问题**：
1. 所有诊断步骤用同一个模型——简单 case 浪费算力，复杂 case 可能不够强
2. 没有成本追踪——不知道每次诊断花了多少钱
3. 没有模型降级策略——如果主模型不可用，没有 fallback

### 11.2 目标

实现**动态模型路由 + 成本可观测 + 降级链**。

### 11.3 具体方案

#### 11.3.1 模型降级链

```python
# doctor/src/llm_factory.py 新增

class FallbackModelChain:
    """模型降级链：主模型不可用时自动切换。

    降级链：o3-mini → gpt-4o → gpt-4o-mini → 报错

    触发条件：
    - API 超时（>30s）
    - Rate limit (429)
    - 模型不可用 (503)
    """

    CHAIN = ["o3-mini", "gpt-4o", "gpt-4o-mini"]

    async def invoke_with_fallback(
        self,
        messages: list,
        primary_model: str,
    ) -> Any:
        chain = [primary_model] + [m for m in self.CHAIN if m != primary_model]

        for model in chain:
            try:
                llm = ChatOpenAI(
                    model=model,
                    api_key=settings.llm_api_key,
                    base_url=settings.llm_base_url,
                    temperature=0.1,
                    timeout=30,
                )
                return await llm.ainvoke(messages)
            except (TimeoutError, RateLimitError, ServiceUnavailableError) as e:
                logger.warning("model_fallback", from_model=model, error=str(e))
                continue

        raise RuntimeError("All models in fallback chain failed")
```

#### 11.3.2 成本追踪

```python
# doctor/src/observability/cost_tracker.py 新建

class CostTracker:
    """追踪每次诊断的 LLM 调用成本。"""

    PRICING = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "o3-mini": {"input": 3.00, "output": 12.00},
    }

    def __init__(self):
        self._records: list[dict] = []

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        cost = self._calculate_cost(model, input_tokens, output_tokens)
        self._records.append({
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        })

    def summary(self) -> dict:
        total_cost = sum(r["cost_usd"] for r in self._records)
        return {
            "total_cost_usd": round(total_cost, 4),
            "total_calls": len(self._records),
        }
```

### 11.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| 模型降级 | 高——可用性 | 0.5d | P2 |
| 成本追踪 | 高——可观测性 | 0.5d | P2 |
| 动态模型路由 | 中——简单 case 省钱 | 1d | P3 |

---

## 12. Hook 系统：工具调用扩展点

> 对应 learn-claude-code s04：在 Agent 循环周围添加扩展点，不修改主循环。

### 12.1 现状

方向 0 的手动循环已经预留了 PreToolUse / PostToolUse 注入点，但还没有实现 Hook 注册和执行机制。

### 12.2 目标

实现 Hook 注册表，支持工具执行前拦截和工具执行后处理。

### 12.3 具体方案

```python
# doctor/src/graph/hooks.py 新建

from typing import Any, Callable

HookFn = Callable[[dict], dict | None]

class HookRegistry:
    """工具调用钩子注册表。"""

    def __init__(self):
        self._pre_hooks: dict[str, list[HookFn]] = {}
        self._post_hooks: dict[str, list[HookFn]] = {}

    def register_pre(self, tool_name: str, fn: HookFn):
        self._pre_hooks.setdefault(tool_name, []).append(fn)

    def register_post(self, tool_name: str, fn: HookFn):
        self._post_hooks.setdefault(tool_name, []).append(fn)

    async def run_pre(self, tool_name: str, args: dict) -> dict:
        for fn in self._pre_hooks.get(tool_name, []):
            result = fn(args)
            if result is None:  # hook 拒绝执行
                raise HookVeto(f"Pre-hook vetoed {tool_name}")
            args = result
        return args

    async def run_post(self, tool_name: str, result: str) -> str:
        for fn in self._post_hooks.get(tool_name, []):
            result = fn(result)
        return result


# 注册示例
registry = HookRegistry()

# PreToolUse: db_query 自动加 LIMIT
def auto_limit_hook(args: dict) -> dict:
    sql = args.get("sql", "")
    if "LIMIT" not in sql.upper() and "SELECT" in sql.upper():
        args["sql"] = f"{sql.rstrip(';')} LIMIT 100"
    return args
registry.register_pre("db_query", auto_limit_hook)

# PostToolUse: search_observability 自动异常检测
def auto_anomaly_hook(result: str) -> str:
    if "trace" in result.lower():
        anomalies = detect_anomalies(result)
        if anomalies:
            result += f"\n\n## 自动异常检测\n{anomalies}"
    return result
registry.register_post("search_observability", auto_anomaly_hook)
```

**改动文件**：`doctor/src/graph/hooks.py`（新建）

### 12.4 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| Hook 注册表 | 高——扩展点基础设施 | 1d | P2 |
| 内置 Hook（auto_limit 等） | 中——立即有用的示例 | 1d | P2 |

---

## 13. Subagent 上下文隔离

> 对应 learn-claude-code s06：大任务拆小，上下文隔离。

### 13.1 现状

当前所有诊断在同一个 Agent 循环中完成。复杂 case（跨层关联 + 多根因）的 context 会快速膨胀，即使有方向 4 的压缩机制，也可能超出单次循环的处理能力。

### 13.2 目标

在**不引入多 Agent 编排**的前提下，支持将特定子任务隔离到独立的 Agent 循环中执行，结果以摘要形式回传主循环。

### 13.3 设计原则

- **不是多 Agent 协作**——主 Agent 仍然是唯一的决策者
- **是上下文隔离**——子任务在独立的 messages 列表中执行，不污染主循环 context
- **结果摘要回传**——子任务完成后只返回结构化结论，不返回完整推理过程

### 13.4 具体方案

```python
# doctor/src/graph/subagent.py 新建

from dataclasses import dataclass


@dataclass
class SubagentTask:
    """子任务定义。"""
    task_id: str
    description: str           # "调查后端 /api/tasks 的 N+1 查询"
    tools_allowed: list[str]   # ["search_observability", "code_search", "get_file_content"]
    max_iterations: int        # 5
    system_prompt_addon: str   # 子任务专属策略


@dataclass
class SubagentResult:
    """子任务结果（摘要回传）。"""
    task_id: str
    findings: str              # 结构化结论
    confidence: float
    tool_calls_made: int
    tokens_used: int


async def run_subagent(
    task: SubagentTask,
    evidence_context: str,
    parent_llm: Any,
) -> SubagentResult:
    """在隔离的 context 中执行子任务。

    与主循环共享 LLM 实例和工具定义，
    但使用独立的 messages 列表。
    """
    messages: list[BaseMessage] = [
        SystemMessage(content=_build_subagent_prompt(task)),
        HumanMessage(content=evidence_context),
    ]

    tools = get_all_tools()
    tool_map = {t.name: t for t in tools if t.name in task.tools_allowed}

    for iteration in range(task.max_iterations):
        response = await parent_llm.ainvoke(messages, tools=list(tool_map.values()))
        messages.append(response)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            tool = tool_map.get(tc["name"])
            if tool:
                result = await tool.ainvoke(tc["args"])
                result = truncate_tool_result(tc["name"], str(result))
                messages.append(ToolMessage(
                    content=result,
                    tool_call_id=tc["id"],
                    name=tc["name"],
                ))

    # 提取摘要（不让完整 messages 回传主循环）
    summary = await _summarize_subagent_result(messages, parent_llm)

    return SubagentResult(
        task_id=task.task_id,
        findings=summary,
        confidence=_extract_confidence(summary),
        tool_calls_made=len([m for m in messages if isinstance(m, ToolMessage)]),
        tokens_used=_estimate_tokens(messages),
    )
```

**主循环中的调用方式**：

```python
# 在方向 0 的手动循环中，当 Agent 判断需要深入调查某个方向时：
# Agent 输出特殊的 tool_call: "delegate_subagent"
# 主循环拦截这个调用，启动 Subagent

if tool_name == "delegate_subagent":
    task = SubagentTask(
        task_id=f"sub-{iteration}",
        description=tool_args["description"],
        tools_allowed=tool_args.get("tools", ["code_search", "get_file_content"]),
        max_iterations=tool_args.get("max_iterations", 5),
        system_prompt_addon=tool_args.get("strategy", ""),
    )
    sub_result = await run_subagent(task, evidence_text, llm)
    # 只把摘要回传主循环
    result = (
        f"[Subagent 结果] {sub_result.findings}\n"
        f"置信度: {sub_result.confidence}\n"
        f"工具调用: {sub_result.tool_calls_made} 次"
    )
```

**改动文件**：
- `doctor/src/graph/subagent.py`（新建）
- `doctor/src/graph/nodes/unified_agent.py`（增加 delegate_subagent 工具调用处理）
- `doctor/src/tools/__init__.py`（注册 delegate_subagent 工具）

### 13.5 适用场景

| 场景 | 子任务 | 隔离价值 |
|------|--------|---------|
| 跨层 Bug | 前端调查和后端调查分开 | 避免 trace 数据污染代码搜索 context |
| 多根因 | 每个根因独立调查 | 避免假设之间的干扰 |
| 大文件分析 | 读取并分析整个文件 | 避免大文件内容占用主循环预算 |
| DB 数据探索 | 多轮 SQL 查询验证 | 避免 DB 结果集膨胀主 context |

### 13.6 优先级评估

| 子项 | 价值 | 工作量 | 优先级 |
|------|------|--------|--------|
| Subagent 执行器 | 高——复杂 case 的 context 隔离 | 2d | P2 |
| delegate_subagent 工具 | 中——Agent 自主决定何时委派 | 1d | P2 |
| 结果摘要回传 | 高——隔离的核心价值 | 1d | P2 |

---

## 总结：实施路线图

### Phase 1（W1）：P0 基础——循环 + 搜索 + 上下文

| 方向 | 子项 | 工作量 |
|------|------|--------|
| 0. 手动循环 | 循环骨架 + 去重 + 错误不中断 | 2d |
| 3. code_search | ripgrep 后端 | 1d |
| 4. 上下文工程 | 工具结果截断 + 历史降级 + 自动压缩 | 2d |
| 1. Ingest | Span 级 N+1 + 置信度 + 烟雾弹 | 2d |
| 2. Observability | 异常检测 + 因果链 | 2.5d |
| **小计** | | **9.5d** |

### Phase 2（W2-W3）：P1 质量——评测 + Prompt + 计划 + Bug Factory

| 方向 | 子项 | 工作量 |
|------|------|--------|
| 5. 评测 | 维度解耦 + 过程评估 + 回归门禁 + 盲集 | 4d |
| 6. Prompt | 动态策略 + Few-shot | 2.5d |
| 7. TodoWrite | 诊断计划 Prompt + 状态解析 | 1d |
| 8. Bug Factory | 变异引擎 + 新增类型 | 5d |
| **小计** | | **12.5d** |

### Phase 3（W4）：P2 鲁棒——安全 + 自省 + 成本 + Hook + Subagent

| 方向 | 子项 | 工作量 |
|------|------|--------|
| 9. 安全 | SQL 复杂度 + 文件白名单 + 输出脱敏 | 2d |
| 10. 自省 | 假设追踪 + 证据覆盖度 + 自我审查 | 2d |
| 11. 成本 | 模型降级 + 成本追踪 | 1d |
| 12. Hook | 注册表 + 内置 Hook | 2d |
| 13. Subagent | 执行器 + delegate 工具 + 摘要回传 | 4d |
| **小计** | | **11d** |

### 总计

| 阶段 | 工作量 | 累计 |
|------|--------|------|
| Phase 1 | 9.5d | 9.5d |
| Phase 2 | 12.5d | 22d |
| Phase 3 | 11d | 33d |

> **建议**：Phase 1 是所有其他方向的基础，必须优先完成。Phase 2 直接提升诊断准确率和评测可信度，ROI 最高。Phase 3 根据实际需求选择性实施。

---

## 附录：与 learn-claude-code Harness 工程的对比

> 参考项目：[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（68.9k stars）
> 核心理念：**Agency Comes from the Model. An Agent Product = Model + Harness.**
> Harness = Tools + Knowledge + Observation + Action Interfaces + Permissions

### 20 个 Harness 机制覆盖度

| # | 机制 | 核心思想 | DiagDoctor 覆盖 | 对应方向 |
|---|------|---------|----------------|---------|
| s01 | Agent Loop | `while True: LLM → tool_use? → execute → loop` | ✅ 已实现 → 方向 0 改为手动 | 0 |
| s02 | Tool Use | dispatch map，工具即 handler | ✅ 已实现 | — |
| s03 | Permission | 先设边界，再给自由 | 🟡 部分覆盖 | 9 |
| s04 | Hooks | PreToolUse / PostToolUse 扩展点 | ❌ → 方向 12 | 12 |
| s05 | TodoWrite | 先规划再执行 | ❌ → 方向 7 | 7 |
| s06 | Subagent | 大任务拆小，上下文隔离 | ❌ → 方向 13 | 13 |
| s07 | Skill Loading | 按需加载知识 | 🟡 部分覆盖 | 6 |
| s08 | Context Compact | 多层压缩，无限会话 | ❌ → 方向 4 | 4 |
| s09 | Memory | 选择 / 提取 / 固化 | 🟡 部分覆盖 | — |
| s10 | System Prompt | 运行时组装，分段拼接 | 🟡 部分覆盖 | 6 |
| s11 | Error Recovery | 重试 / 让路 / 换路径 | 🟡 部分覆盖 | 10+11 |
| s12 | Task System | 文件持久化的任务依赖图 | ❌ 不适用 | — |
| s13 | Background Tasks | 慢操作后台化 | ❌ 不适用 | — |
| s14 | Cron Scheduler | 定时触发 | ❌ 不适用 | — |
| s15 | Agent Teams | 持久队友 + 异步邮箱 | ❌ 不适用 | — |
| s16 | Team Protocols | 固定请求-回复格式 | ❌ 不适用 | — |
| s17 | Autonomous Agents | 空闲自检 | ❌ 不适用 | — |
| s18 | Worktree Isolation | 任务绑定目录 | ❌ 不适用 | — |
| s19 | MCP Plugin | 外部工具接入 | ❌ 不适用 | — |
| s20 | Comprehensive | 多机制围绕一个循环 | 🟡 部分覆盖 | 全部 |

**统计**：
- ✅ 已实现：2 个（s01, s02）
- 🟡 部分覆盖：5 个（s03, s07, s09, s10, s11）
- ❌ 缺失但已规划：5 个（s04→方向12, s05→方向7, s06→方向13, s08→方向4, s20）
- ❌ 不适用：8 个（s12-s19 中除 s19 外大部分不适用于诊断场景）

### DiagDoctor 的 Harness 公式

```
Harness = Tools(5个, ✅)
        + Knowledge(struct_kb + few-shot, 🟡)
        + Observation(Ingest 9步管线, ✅)
        + Action Interfaces(API, 🟡)
        + Permissions(SQL守卫+路径沙箱+脱敏, 🟡)
        + Planning(→方向7)
        + Context Mgmt(→方向4)
        + Hooks(→方向12)
        + Subagent(→方向13)
        + Error Recovery(→方向10+11)
```

**目标**：通过 14 个方向的逐步实施，将 DiagDoctor 从"能用的 Agent"升级为"工程化的 Harness"——不是靠堆复杂度，而是靠每个 harness 机制的精准设计。

---

## 附录 D：数据获取策略——性能与缓存设计

> 回答"是否应该缓存更多日志到内存/记忆中，Agent 先从缓存查"的问题。

### D.1 性能瓶颈分析

一次诊断的耗时拆解：

```
Loki 查询         ~0.5s    ▏ 2%
Tempo 查询        ~1s      ▏ 3%
Ingest 标准化     ~0.2s    ▏ 1%
LLM 推理（每轮）   ~3-8s   ████████████ 60%
× 6-8 轮工具调用            ████████████████████████ 34%
─────────────────────────────────────
总耗时             ~30-60s
```

**结论**：Loki/Tempo 查询占比不到 5%。缓存原始日志对整体延迟几乎无影响。真正瓶颈是 LLM 推理——上下文工程（方向 4）才是正确优化方向。

### D.2 为什么不应该"缓存更多日志"

| 方案 | 问题 |
|------|------|
| 全量缓存 raw data 到内存 | context 爆炸 → LLM 推理质量退化 → 幻觉率上升 |
| 缓存后 Agent "从记忆中搜索" | Agent 不是搜索引擎，应该在 Ingest 拿到方向后**精准查询**，而非翻找缓存 |
| 用向量检索日志 | 日志的语义向量不可靠——"timeout"和"connection refused"语义相似但根因完全不同 |

### D.3 推荐策略：三层分层查询

```
L1: NormalizedEvidence（Ingest 产物，内存）
    ├─ golden_signals（5-15 个结构化信号）
    ├─ correlations（跨层关联）
    └─ raw_refs（已拉取数据的内存索引）
    → Agent 开局就有，不需要调工具
    → 命中率：开局 100%，后续查询低

L2: 工具结果语义缓存（Agent 循环内）
    ├─ 缓存 key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
    └─ 相同参数重复调用 → 直接返回缓存结果
    → Phase 2 实现，与现有去重互补

L3: Loki/Tempo 实时查询
    → 精确查询（按 trace_id / span_id / 关键词）
    → 这才是正确使用方式——Agent 带着明确问题去查
```

### D.4 工具结果语义缓存（轻量，推荐 Phase 2 实现）

```python
# 在 Agent 循环中，工具调用前检查缓存
_tool_cache: dict[str, str] = {}

for tc in response.tool_calls:
    cache_key = f"{tc['name']}:{json.dumps(tc['args'], sort_keys=True)}"
    if cache_key in _tool_cache:
        result = _tool_cache[cache_key]  # 0ms 命中
    else:
        result = await tool_map[tc['name']].ainvoke(tc['args'])
        _tool_cache[cache_key] = str(result)

    messages.append(ToolMessage(content=str(result), ...))
```

与工具调用去重的区别：
- **去重**：阻止完全相同的 (tool_name, args) 重复执行
- **缓存**：阻止语义相似的查询重复执行（不同参数但相同意图）
- 缓存命中时仍返回 ToolMessage（不跳过），只是不调真实工具

### D.5 优先级评估

| 方案 | 复杂度 | 收益 | 推荐 |
|------|--------|------|------|
| 多拉日志全量缓存 | 低 | **负**（context 爆炸） | ❌ |
| raw_refs cache-aside | 极低 | 微（重复查询少） | 🟡 顺手做 |
| 工具结果语义缓存 | 中 | 中（节省重复 LLM 轮次） | 🟢 Phase 2 |
| L1→L2→L3 分层查询 | 已接近 | 已体现 | ✅ 当前架构 |
