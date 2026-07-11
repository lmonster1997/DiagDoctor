# DiagDoctor Harness 迭代优化日志

> 分支：`dev-harness-redesign`（从 `main` 拉出，含 2 个 LLM observability 修复 commit）
> 起始日期：2026-07-05
> 目的：把 DiagDoctor 诊断 Agent 的 harness 从「过度设计的多层防御」重做成「case 驱动的增量机制」。
> 用途：作为面试叙事材料——展示如何用证据驱动的方法做 harness 工程，而不是理论先行。
>
> 📌 **文件路径说明（2026-07 更新）**：本日志早期章节中引用的
> `doctor/src/graph/nodes/unified_agent.py` 已在 V3 重构中拆分为
> `doctor/src/graph/nodes/diagnosis_agent/` 包（入口 `node.py` 的
> `diagnosis_agent_node`，循环逻辑在 `react_loop.py`，强制终态在
> `forced_call.py`）。后续 Iteration 2 章节已直接引用新路径；早期章节保留
> 旧路径名以保留迭代历史的原貌，阅读时请按上述映射理解。

---

## 第 1 章 · Baseline 现状

### 1.1 为什么需要 baseline

第一版基于「abductive completeness / Occam / competitive testing」等概念一次性堆了 4 层机制（S1 预算兜底 / S2 跨层收敛 / S1.5 flailing / S3 abductive completeness）。跑 4 个 smoke case 后看 Langfuse trace：

| Case | overall | 真实失败模式 |
|---|---|---|
| BE-020 | 0.97 | 完美——agent 自然交付 JSON，所有机制都没触发 |
| LOGIC-020 | 0.97 | 完美——同上 |
| FE-020 | 0.44 | agent **第 2 轮就找到根因**，但被推进 REPORTING phase 后 DeepSeek 卡在 DSML 工具调用模式，连 S1 forced call 也吐 DSML，最终 harness 合成低质量报告 |
| PERF-020 | 0.64 | 类似 FE-020——agent 找到 N+1 根因但没产出 JSON |

**核心问题**：理论先行、多层防御互相干扰、机制没对应具体失败、REPORTING phase 设计假设错了（DeepSeek 在对话历史有 tool_calls 时会持续输出 DSML，prompt 禁不掉）、收敛检测过早触发。

**教训**：harness 工程的正确姿势是 case 驱动，不是概念驱动。每一行 harness 代码必须能指着一个具体 case 的具体轮次说「这里如果不加这个机制会失败」。

→ 决定砍光所有非必要机制，从 baseline 重新开始，按 case 失败严重度增量加机制。

### 1.2 保留了什么（必要基础设施）

| 项 | 实现位置 | 理由 |
|---|---|---|
| **硬约束：迭代上限 `MAX_TOOL_CALLS=12`** | `unified_agent.py` 模块常量 | `for iteration in range(MAX_TOOL_CALLS)` 强制 cap，防止 agent 无限循环 |
| **硬约束：token 上限 `MAX_TOKENS_BUDGET=100_000`** | 同上 | 循环内 `ctx_budget.total_used >= MAX_TOKENS_BUDGET` 即 break |
| **硬约束：时间上限 `MAX_TIME_SECONDS=300`** | 同上 | 循环内 `ctx_budget.elapsed_seconds >= MAX_TIME_SECONDS` 即 break |
| **基础 ReAct 循环** | `unified_agent_node` | `llm_with_tools.ainvoke` → 执行 tool_calls → ToolMessage 入 messages → 下一轮 |
| **工具调用去重** | `call_history: list[tuple[str, str]]` | 按 `(tool_name, json.dumps(args, sort_keys=True))` 去重，重复调用直接 append `[跳过]` ToolMessage。效率优化，不改变 agent 决策 |
| **静态工具结果截断** | `truncate_tool_result(tool_name, str(result))` | 按工具类型字符上限（`search_observability=6000` / `code_search=4000` / `get_file_content=8000` / `db_query=3200`），超限优先保留关键行（error/exception/trace/span 等关键词）。防单条结果撑爆 context，静态规则不影响可重复性 |
| **`ContextBudget` token 计数（仅 telemetry）** | `context_engine.py` | 调用 `add_system_prompt` / `add_evidence` / `add_agent_reasoning` / `add_tool_result` / `add_tool_call` / `tick_iteration` / `start_timer` 累计 token，仅供硬约束检查和日志，**不驱动任何 phase 决策** |
| **Langfuse tracing** | `unified_agent_node` | `start_trace` / `record_tool_span` / `record_tool_skipped` / `end_trace`——必须能看到 agent 在干什么 |
| **`handle_agent_failure` 异常兜底** | `unified_agent.py` | 循环外层 `try/except Exception` 捕获未预期异常，返回 `primary_category=""` / `confidence=0.0` / `early_stopped=True` 的报告 |
| **`parse_diagnosis_report` + `extract_findings`** | 同上 | 解析最后一条 AIMessage 为 JSON → `DiagnosisReport`；提取 findings。复用现有解析逻辑 |
| **空报告兜底（最小）** | `unified_agent_node` 末尾 | `report is None` → 给 `primary_category=""` / `confidence=0.3` / `notes="Agent 未输出有效 JSON"` 的报告。**不做合成，不做 forced call** |
| **`early_stopped` 标记** | `is_budget_exceeded(budget_state) or budget_exhausted` | 标记是否因预算耗尽提前终止，写入 report.early_stopped 和 notes |

### 1.3 砍了什么（待 case 驱动重新加）

| 机制 | 原作用 | 砍的理由 |
|---|---|---|
| **`_detect_convergence`** | 跨层收敛判据：≥3 distinct files + ≥2 distinct layers + signal evidence + crash code loc + 因果词 → 进 REPORTING phase | 收敛检测核心——先看 agent 何时自然收敛 |
| **`_detect_flailing`** | 连续 N 轮 code-loc-only 无新 signal，或反复读同一文件 → 注入 nudge | 同上 |
| **`ContextPhase` 5 阶段枚举** | INITIAL / INVESTIGATING / CONVERGING / REPORTING / FINALIZING | phase 是收敛检测的载体，先砍看纯行为 |
| **`build_dynamic_system_prompt`** | 按 phase 注入策略文本 + 预算状态 + 诊断进展 | 依赖 phase，phase 砍了它没意义 |
| **`maybe_compact_context`** | `usage_ratio > 60%` 降级旧工具消息，`> 75%` 截断到 500 字符 | 跟收敛一样，先看 baseline 行为再加 |
| **`degrade_old_tool_results`** | 保留近 N 条原文，次近摘要，更早归档 | `maybe_compact_context` 的子例程 |
| **REPORTING phase 逻辑** | 禁工具 + 注入 nudge + 保留 2 轮写 JSON | 第一版失败主因（DSML 污染） |
| **FINALIZING phase 逻辑** | 警告注入 + 强制 stop | phase 砍了自然没了 |
| **S1 forced final call** | 无工具的强制最终 LLM 调用，带 hypothesis hint | 多层兜底——先看 agent 自然 stop 时能否交付 JSON |
| **`_synthesize_fallback_report`** | 从 `running_hypothesis` + 历史合成完整报告 | S1 的子机制 |
| **`_update_running_hypothesis` / `_format_hypothesis_hint`** | 每轮从 AIMessage 提取 partial JSON 字段 + 最强 root_cause 叙述 + 引用过的 file 路径 | 只为 S1 用 |
| **`_report_is_incomplete`** | 检查报告字段完整性 | 只为 S1 用 |
| **`running_hypothesis` 状态 + 相关标志** | `convergence_nudged` / `flailing_warned` / `has_signal_evidence` / `has_code_loc` / `iteration_tool_categories` / `probed_files_counts` / `finalizing_warned` | 全是 S1/S2/S1.5 的运行时状态 |
| **`explained_signals` / `missing_signals` 字段** | `DiagnosisReport` 上的 abductive completeness 字段 | S3 的产物，已从 `state.py` 移除 |

### 1.4 主循环伪代码

```python
async def unified_agent_node(state):
    evidence = state.evidence
    evidence_text = format_evidence_for_agent(evidence)

    base_prompt = _build_system_prompt()
    messages = [SystemMessage(base_prompt), HumanMessage(evidence_text)]

    llm = get_llm_for_role("diagnosis")
    tools = get_all_tools()
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    call_history = []  # 工具去重
    ctx_budget = ContextBudget()  # 仅 telemetry
    ctx_budget.add_system_prompt(base_prompt)
    ctx_budget.add_evidence(evidence_text)
    ctx_budget.start_timer()

    # Langfuse tracing 初始化（graceful degradation）

    budget_exhausted = False
    try:
        for iteration in range(12):  # MAX_TOOL_CALLS
            ctx_budget.tick_iteration()

            # 硬约束：token / time
            if (ctx_budget.total_used >= 100_000
                or ctx_budget.elapsed_seconds >= 300):
                budget_exhausted = True
                break

            response = await asyncio.wait_for(
                llm_with_tools.ainvoke(messages, config=invoke_config),
                timeout=300,
            )
            messages.append(response)
            ctx_budget.add_agent_reasoning(str(response.content))

            if not response.tool_calls:
                break  # agent 自然 stop

            for tc in response.tool_calls:
                call_key = (tc["name"], json.dumps(tc["args"], sort_keys=True))
                if call_key in call_history:
                    messages.append(ToolMessage("[跳过：与之前调用完全相同]", ...))
                    continue
                call_history.append(call_key)

                try:
                    result = await tool_map[tc["name"]].ainvoke(tc["args"])
                except Exception as e:
                    result = f"工具执行错误: {e}"

                result_str = truncate_tool_result(tc["name"], str(result))
                ctx_budget.add_tool_call(1)
                ctx_budget.add_tool_result(result_str)
                # record_tool_span(...)
                messages.append(ToolMessage(result_str, ...))
        else:
            budget_exhausted = True  # for-else：跑满 12 轮未 break

    except Exception as exc:
        return handle_agent_failure(state, exc)  # 异常兜底

    # 解析输出（baseline：不兜底，parse 失败就给空报告）
    report = parse_diagnosis_report({"messages": messages})
    findings = extract_findings({"messages": messages})
    budget_state = update_budget(state.budget, {"messages": messages})
    early_stopped = is_budget_exceeded(budget_state) or budget_exhausted

    if report is None:
        report = DiagnosisReport(
            primary_category="",
            root_cause=findings[0].summary if findings else "诊断未完成",
            confidence=0.3,
            early_stopped=early_stopped,
            notes="Agent 未输出有效 JSON",
        )
    if early_stopped:
        report.early_stopped = True
        if not report.notes:
            report.notes = "预算超限，提前终止诊断"

    # Langfuse end_trace(...)
    return {"report": report, "findings": findings, "budget": budget_state, "early_stopped": early_stopped}
```

### 1.5 当前文件状态

| 文件 | 状态 | 说明 |
|---|---|---|
| `doctor/src/graph/nodes/unified_agent.py` | **已重写** | 从 ~1464 行 → 835 行。砍掉 ~420 行 S1/S2 helpers + ~300 行 main loop 中的 phase/nudge/fallback 逻辑。Import 移除 `ContextPhase` / `build_dynamic_system_prompt` / `maybe_compact_context` |
| `doctor/src/graph/context_engine.py` | **未动** | `ContextPhase` / `maybe_compact_context` / `build_dynamic_system_prompt` / `degrade_old_tool_results` 仍在文件里，但 `unified_agent_node` 不再调用 → dead code。相关测试 `test_context_engine.py` 仍能跑（有几个 pre-existing 失败，与本次改动无关） |
| `doctor/src/graph/state.py` | **干净** | 已确认无 `explained_signals` / `missing_signals` 字段 |
| `doctor/tests/graph/test_abductive_completeness.py` | **不存在** | S3 测试已被 revert |
| `doctor/scripts/dump_s3_abductive_check.py` | **已删除** | S3 专用分析脚本，不再需要 |
| `doctor/scripts/dump_trace_llm_responses.py` | **保留** | 通用调试工具——看一个 trace 的所有 LLM call 输入输出，case 驱动调试的眼睛 |

### 1.6 预期 baseline 行为（待 smoke 验证）

agent 在无 harness 干预下的可能行为：

1. **自然 stop**：agent 输出无 tool_calls 的 AIMessage → parse 那条为 JSON。理想路径。
2. **跑满 12 轮**：agent 一直调工具直到迭代上限 → `budget_exhausted=True`，parse 最后一条 AIMessage。
3. **触发硬约束**：token 超 100k 或时间超 300s → `budget_exhausted=True`，break，parse 最后一条。
4. **parse 失败**：最后一条 AIMessage 不是合法 JSON（narrative 文本 / 空 content）→ fallback 报告（`primary_category=""` / `confidence=0.2`，root_cause 塞原始文本）。
5. **异常**：未预期异常 → `handle_agent_failure` 返回 `confidence=0.0` 报告。

**已知风险（从第一版 trace 已观测；baseline 实际验证见第 3 章 Iteration 0）**：
- agent 找到根因后可能继续调工具直到 12 轮耗尽（无早交付机制）→ 末轮 content 空 → 空报告
- agent 即便自然 stop，也可能输出 narrative 散文而非 JSON → parse 失败
- 没有 forced call，parse 失败就直接 fallback 空报告（无兜底合成）

→ 这些正是后续 iteration 要逐个 case 驱动解决的。

### 1.7 硬约束常量

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_TOOL_CALLS` | 12 | 迭代轮数上限（一轮回调 1+ 个工具） |
| `MAX_TOKENS_BUDGET` | 100_000 | system_prompt + evidence + tool_result + agent_reasoning 累计 token 上限 |
| `MAX_TIME_SECONDS` | 300 | 单次诊断总时间上限（含所有 LLM call + 工具执行） |

这些值是第一版留下的，**baseline 跑完后根据实际 token/time 分布再调**——可能 12 轮对某些 case 太少，可能 100k token 永远到不了。

---

## 第 2 章 · 方法论

### 2.1 工作流（case 驱动增量设计）

```
1. 跑 smoke（4 case：BE-020 / FE-020 / LOGIC-020 / PERF-020）
2. 用 dump_trace_llm_responses.py 看每个 case 的 LLM 输出序列
3. 找最差的 case，定位它的具体失败模式（哪一轮、什么行为、为什么）
4. 设计最小机制针对那个失败
5. 实现 + 加单元测试覆盖那个机制
6. 重跑 smoke 对比
7. 改善且不破坏其他 case → 保留；有副作用 → 回滚或调整
8. 回到 1
```

### 2.2 纪律

- **每个机制对应一个观测到的失败**：不许「以防万一」式防御
- **一次只加一个机制**：才能归因效果
- **每次跑完记录到本文档**：case → 失败模式 → 机制 → 测量 → 结论
- **保留有用的工具脚本**：`dump_trace_llm_responses.py` 是 case 驱动调试的眼睛

### 2.3 Harness 8 维度全景

按对当前 4 个 smoke case 的预期影响力排序：

| # | 维度 | 解决什么 | 当前 baseline 状态 |
|---|---|---|---|
| 1 | **收敛 / 停止** | 何时算完、何时强切 | 砍光，只剩硬约束 |
| 2 | **输出格式 / 解析** | 让 LLM 吐出可 parse 的 JSON | 砍光——baseline 4 个 disaster 全栽在这：3 个跑到 cap 末轮 content 空、1 个自然 stop 但输出散文 |
| 3 | **上下文工程** | 每轮 LLM 看到什么 | 砍光，只剩静态截断 |
| 4 | **Prompt 工程** | system prompt 怎么写、工具怎么描述 | 未动——`unified_agent.j2` + `tools_reference.md` 还是原版 |
| 5 | **工具行为** | 工具调用去重、错误信息、并行、参数校验 | 仅留去重；错误信息是 raw exception 字符串 |
| 6 | **循环编排** | ReAct 单循环 vs 子图 vs 重试 | 未动——目前是单层 ReAct |
| 7 | **预算 / 模型路由** | 哪个角色用哪个模型、cost cap | 仅留硬 token cap；模型路由是 `get_llm_for_role` 单一 |
| 8 | **失败恢复 / 健壮性** | API timeout、malformed args、工具异常 | 仅留 `handle_agent_failure` 兜底 |

**预期占比（凭 4 个 smoke 的失败模式预判；实际 baseline 发现见第 3 章 Iteration 0）**：

```
输出格式  ████████  ←  4 个 disaster 全栽在这（空 content / 散文，不是 DSML）
收敛      ██████    ←  agent 找到根因后何时停、停了怎么落 JSON
上下文    ████      ←  token 不够时怎么压、压了怎么不让 nudge 失语境
Prompt    ███       ←  system prompt 里的"输出 JSON"指令现在太弱
工具行为  ██        ←  工具错误信息现在是 raw exception，LLM 看不懂
循环编排  █         ←  目前单循环够用，多 agent 是后期事
模型路由  █         ←  换模型可能改善交付，但 baseline 数据显示不是模型 bug
失败恢复  █         ←  edge case 兜底
```

### 2.4 为什么不按维度分阶段做（耦合陷阱）

「先做完收敛，再做上下文」这种顺序**会埋雷**。两个维度至少有三处耦合：

| 耦合 | 例子 |
|---|---|
| 收敛判据依赖上下文内容 | 收敛 nudge 引用"第 5 轮读到的 stack trace"，但下一版上下文压缩把那条 ToolMessage 降级成 `[已归档]`，nudge 语境就断了 |
| 上下文压缩触发依赖预算 | `maybe_compact_context` 在 `usage_ratio > 60%` 触发；收敛早交付会减少 token 消耗 → 永远触发不了压缩 → 你优化好的压缩机制在收敛好的 case 上根本不跑 |
| 输出格式依赖对话历史 | forced final call 的上下文该用什么——既不是纯收敛（什么时候触发）也不是纯上下文（喂什么内容），属于两个维度的交叉地带 |

→ **正确做法：按 case 失败严重度排序，不按机制分类。** 每次取最严重的失败模式来修，不管修的是收敛、上下文还是输出格式——修完跑全量 smoke，4 个 case 都不能退步，才合并。一个失败模式可能需要同时改收敛和上下文——这没问题，反正每次只改一个失败模式。

### 2.5 反直觉建议：先验证模型假设，再做机制

维度 7（模型路由）虽然画了 1 格，但**它可能是真正的第二步**——在投入做输出格式机制之前，先花一次 iteration 跑一个对照实验：同样的 baseline，把 diagnosis 模型换成 GPT-4o-mini 跑 4 个 case。

- 如果分数立刻飙升 → 输出格式问题是 DeepSeek 特有问题（比如它更容易「停不下来」或「输出散文而非 JSON」），"输出格式"维度大半可以省下来，直接换成"在 forced call 时切 GPT-4o-mini"这种轻机制
- 如果换了还是烂 → 问题真是 harness 设计问题，再扎进去做机制

→ **用最便宜的实验砍掉一整个维度的工作量**，比闷头写格式约束机制高效得多。

---

## 第 3 章 · 迭代日志

> 每次加机制后跑 smoke，按以下模板记录。
> 失败的实验也要记——面试时讲「我试了 X，因为 Y 砍了」比「我都做对了」更有说服力。

### Iteration 0: Baseline 跑通

- **日期**：2026-07-05
- **改动**：无（baseline 已就绪，见第 1 章）
- **Run**：`baseline-15case`（15 case 全量，session_id 在 Langfuse）
- **15 case 分数**：

| bug_id | overall | root | cat | file | fix | line | evid | conf | proc | 类别 | 难度 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **BE-020** | **0.04** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.70 | 0.83 | backend_error | L1 | smoke；跑到 12 轮 cap，空报告 |
| **BE-021** | **0.31** | 0.95 | 0.00 | 0.00 | 0.00 | 0.00 | 0.15 | 0.25 | 1.00 | backend_error | L2 | root 对了但 JSON parse 失败 |
| **BE-022** | 0.95 | 0.95 | 1.00 | 1.00 | 0.85 | 1.00 | 0.95 | 0.98 | 1.00 | backend_error | L3 | red-herring，自然 stop |
| **CASCADE-020** | 0.68 | 0.65 | 0.50 | 1.00 | 0.55 | 0.50 | 0.95 | 0.67 | 1.00 | performance | L4 | 复合 case，category 选错 |
| **CONFIG-020** | 0.97 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.95 | 0.97 | 0.83 | config | L3 | misleading-default，几乎完美 |
| **DATA-020** | 0.96 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.85 | 0.97 | 1.00 | data | L2 | 自然 stop |
| **DATA-021** | 0.95 | 0.95 | 1.00 | 1.00 | 1.00 | 1.00 | 0.70 | 0.95 | 1.00 | data | L2 | 自然 stop |
| **FE-020** | **0.04** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.70 | 0.83 | frontend_crash | L4 | smoke；跑到 cap，空报告 |
| **FE-021** | **0.04** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.70 | 1.00 | frontend_crash | L2 | 跑到 cap，第 1 轮 4 个并发 search |
| **LOGIC-020** | 0.94 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.70 | 0.97 | 1.00 | logic | L2 | smoke；自然 stop |
| **LOGIC-021** | 0.96 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.85 | 0.95 | 1.00 | logic | L3 | red-herring，5 轮自然 stop |
| **LOGIC-022** | 0.94 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.70 | 0.96 | 1.00 | logic | L2 | silent-drop，自然 stop |
| **PERF-020** | 0.83 | 0.85 | 1.00 | 1.00 | 0.95 | 0.00 | 0.95 | 0.90 | 1.00 | performance | L1 | smoke；line=0（N+1 无单一行号） |
| **PERF-021** | 0.91 | 0.95 | 1.00 | 1.00 | 0.95 | 0.50 | 0.85 | 0.97 | 1.00 | performance | L2 | line=0.5（同上） |
| **RACE-020** | 0.87 | 0.95 | 1.00 | 1.00 | 0.65 | 1.00 | 0.60 | 0.97 | 1.00 | logic | L4 | smokeless；fix=0.65（乐观锁建议弱） |

- **聚合统计**：
  - overall mean=**0.693**，min=0.04，max=0.97
  - **4 个 disaster**（overall<0.4）：BE-020 / BE-021 / FE-020 / FE-021
  - 11 个 healthy（0.68-0.97）
  - 各维度均值：root=0.733 / cat=0.700 / file=0.733 / fix=0.647 / line=0.600 / evid=0.613 / conf=0.841 / **proc=0.967**

- **观察**：
  - **自然 stop vs 跑满 12 轮**：清晰的二分。验证了 4 个 disaster case 的 LLM call 序列：
    - BE-020（disaster）：12 LLM + 15 tools，**末轮 content=0 chars（空）** → 跑到 cap，最后一条 AIMessage 是纯 tool_call 消息（content 空，所有输出在 `tool_calls` 字段）
    - FE-020（disaster）：12 LLM + 20 tools，**末轮 content=0 chars（空）** → 同上
    - FE-021（disaster）：12 LLM + 21 tools，**末轮 content=0 chars（空）** → 同上
    - BE-021（disaster，**关键反例**）：**仅 11 LLM + 16 tools**，**末轮 content=1376 chars 叙事散文** → 第 11 轮**自然 stop 了**，但输出的是解释根因的散文，不是 JSON
    - LOGIC-021（healthy）：5 LLM + 7 tools，末轮是 `llm_call_5`（无后续 tool）→ 第 5 轮 natural stop，parse 那条为 JSON 成功
  - **最后一条 AIMessage 是什么**：4 个 disaster 分两种失败模式：
    1. **跑到 cap + 末轮空 content**（3/4：BE-020 / FE-020 / FE-021）：agent 一直调工具到 12 轮上限，末轮 LLM 把所有输出放进 `tool_calls` 字段，`content=""`。`parse_diagnosis_report` 拿到空字符串 → `_extract_json_from_text("")` → None → fallback 空报告（`primary_category=""` / `confidence=0.2`）
    2. **自然 stop + narrative 不带 JSON**（1/4：BE-021）：agent 在第 11 轮自然 stop（不再调工具），但 1376 字 content 是叙事散文解释根因，不含 JSON 结构。`_extract_json_from_text` 找不到 JSON → fallback。这解释了 BE-021 的 `root_cause_accuracy=0.95`——LLM judge 从 fallback 的 `root_cause=last_ai_content[:500]` 字段里读到了正确根因，但 `category/file/fix/line` 全 0 因为没有结构化字段
  - **token / time / iteration 分布**：从 trace 时间戳看，disaster case 整体耗时 20-30s（BE-020 20s，FE-021 27s），未触发 100k token / 300s 硬约束——**所有 disaster 都是 iteration cap (12 轮) 触发的，不是预算触发的**（BE-021 除外，它是自然 stop 但格式错）
  - **proc=0.967 的反直觉**：过程质量几乎满分说明 agent 的查证方法本身没问题（用了 signal + code + db 三类工具，无真重复），问题不在「怎么查」而在「查完怎么交付」

- **与第一版（带 4 层 harness）的 smoke 4 case 对比**：

| case | 第一版（带 harness） | baseline（无 harness） | 解读 |
|---|---|---|---|
| BE-020 | 0.97 | **0.04** | 第一版的 REPORTING phase 在这里**真的有用**——强制交付 JSON 拯救了 easy case |
| LOGIC-020 | 0.97 | 0.94 | 微降，harness 几乎无影响 |
| FE-020 | 0.44 | **0.04** | 第一版至少有低质量报告（0.44），baseline 连报告都没有 |
| PERF-020 | 0.64 | **0.83** | **baseline 反而更好**——第一版的 REPORTING phase 干扰了原本能自然交付的 case |

- **结论**：
  - **baseline 表现**：overall mean 0.693，两极分化严重（4 个 0.04-0.31 + 11 个 0.68-0.97）。**「难 case 全过、易 case 全崩」的反直觉分布**——L3/L4 的 red-herring / smokeless / config 类全过，L1/L2 的 backend_error / frontend_crash 类全崩。
  - **最差的 case**：BE-020 / FE-020 / FE-021（overall=0.04 并列最低）。BE-021 (0.31) 是次差但诊断价值最高——它证明了 agent **找到了根因、也自然停止了，但交付的不是 JSON 格式**。
  - **核心失败模式（两种）**：
    1. **跑到 cap + 末轮空 content**（3/4 disaster）：agent 一直调工具到 12 轮上限，末轮 LLM 输出 `content=""` + `tool_calls=[...]`。loop 因 `range(12)` 耗尽而 break，`parse_diagnosis_report` 拿到空 content → 空报告
    2. **自然 stop + narrative 不带 JSON**（1/4 disaster，BE-021）：agent 自然停止调工具，但输出的是 1376 字叙事散文，不含 JSON 结构
  - **为什么易 case 反而崩、难 case 反而过**：难 case（logic / data / config）的根因查证路径明确——agent 查到关键证据后**自然会停止推理并输出 JSON**。易 case（backend_error / frontend_crash）的根因在错误堆栈里直接可见——agent 找到后**信心不足**，继续调更多工具「再确认一下」，停不下来，直到 cap；即便停下来的（BE-021）也输出散文而非 JSON。
  - **下一步该改哪个维度**：**输出格式 / 收敛**（第 2.3 节维度 1 + 2，最高优先级）。基于两种失败模式，**单一机制即可同时覆盖**：
    - **统一的「stop 后 forced final JSON call」**：不论 agent 是自然 stop 还是跑到 cap，在 loop 结束后做一次**额外 LLM call**，prompt 明确要求「基于已收集的证据输出 DiagnosisReport JSON，不要再调任何工具」。这一招同时救：
      - failure mode 1（3/4 disaster）：cap 后给 agent 一次「只能输出 content、不能调工具」的机会，把 `content=""` 变成 `content=<JSON>`
      - failure mode 2（1/4 disaster，BE-021）：自然 stop 后给 agent 一次「请把你的叙事格式化成 JSON」的机会
    - 这个机制比第一版的 REPORTING phase 简单得多——不需要 phase 状态机、不需要收敛检测、不需要 nudge 注入；只需在 loop 末尾加一次 LLM call，prompt 强制「no tools, JSON only」
  - **面试叙事点**：baseline 跑出来「难 case 全过、易 case 全崩」是个反直觉发现——直觉上易 case 应该最稳。这揭露了诊断 Agent 的一个本质特征：**harness 的价值不在「帮 agent 推理」（agent 推理能力本来就够），而在「帮 agent 知道何时停止推理并交付」**。第一版的 4 层机制方向错了——它在「帮 agent 推理得更好」，而真问题是「帮 agent 交付得出来」。case 驱动方法的价值就在这里：不跑 baseline 你看不见这个反转。

#### 1.8 Baseline 方差观测（2026-07-05 复跑）

同一套代码、同一批 15 case，第二次跑 `baseline-15case-20260705-155209`：

- overall mean = **0.666**（vs 首跑 0.693，Δ=−0.027）
- disaster（overall<0.4）= **5 个**（vs 首跑 4 个），但**组成变了**：
  - 出灾难区：BE-021 0.31→0.97（自然 stop 这次恰好交付了 JSON）
  - 进灾难区：CONFIG-020 0.97→0.30、PERF-021 0.91→0.30（这两个 root_cause_accuracy 仍是 0.95，只是这次没把结论落成 JSON——`notes="JSON 解析失败"`）
- 各维度均值：root=0.810 / cat=0.533 / file=0.667 / fix=0.607 / line=0.533 / evid=0.557 / conf=0.791 / proc=0.933

**关键结论**：

1. **两版差距纯属 LLM 方差，不是任何机制改动驱动**。两次跑之间唯一改过的是 `code_search` 的 Windows 事件循环 bug fix（`asyncio.create_subprocess_exec` → `subprocess.run` + `asyncio.to_thread`，见 commit 历史），但首跑 baseline 时 `code_search` 本就是好的——那个 bug 只在中间一次单 case 跑（`all-20260705-153450_BE-020`）里出现过，不进两次 baseline 对比。所以 −0.027 不能归因给 fix。
2. **方差主要落在"JSON 交付"环节，不落在"推理"环节**。两个回归 case 的 root_cause_accuracy 都还是 0.95，掉的全是 cat/file/fix/line 这些需要结构化 JSON 才能拿分的维度。这进一步收紧了第 3 章 Iteration 0 的结论：**失败模式不是 case 固有属性，而是"这一轮 LLM 恰好有没有交付 JSON"的高方差随机事件**——CONFIG-020（L3）和 PERF-021（L2）上一轮过、这轮崩，证明它跟难易度无关。
3. **baseline 自身噪声 ≈ ±0.03（overall mean 量级），disaster 归属都不稳定**。后续 iteration 的 improvement 必须超过这个方差带才算信号，否则可能是噪声。Iteration 1（forced JSON）如果能把 disaster 数从 5 稳定压到 ≤2、overall mean 提到 ≥0.75，才算真生效。

---

### Iteration N: [机制名]（模板）

- **日期**：
- **维度**：收敛 / 上下文 / 输出格式 / Prompt / 工具行为 / 循环编排 / 模型路由 / 失败恢复
- **针对的 case**：[最差 case 的 ID]
- **观测到的失败模式**：
  - 第几轮失败：
  - 失败行为：
  - 为什么没被现有机制拦住：
- **加的机制**：
  - 代码位置：
  - 逻辑简述：
  - 为什么是最小改动：
- **单元测试**：
  - 测试文件：
  - 覆盖的场景：
- **smoke 重跑结果**：
  - 4 case 分数对比表：
  - 那个最差 case 改善了吗：
  - 其他 case 退步了吗：
- **结论**：保留 / 回滚 / 调整
- **面试叙事点**：这个机制展示了我对 X 的理解，体现在 Y 设计决策上

---

## 第 4 章 · 已尝试但回滚的机制（graveyard）

> 记录失败的实验。面试时讲「我试了 X，跑了 smoke 发现 Y 副作用，所以回滚了」——这是工程成熟度的证据。

### Iteration 2 计划方案：`json_repair.repair_json()` fallback（未实施，换方案）

- **计划**：在 `_extract_json_from_text` 的 `json.loads` 失败路径后加 `json_repair.repair_json()` fallback
- **为什么换方案**：`json_repair` 是事后修补，需要 LLM 先产出错的 JSON 再 fix；`with_structured_output`
  是事前预防，schema 在 API 层强制。治本优于治标，且不需要新依赖
- **保留为 graveyard 而非删除**：如果未来遇到 `with_structured_output` 也救不了的 case（比如模型
  拒绝 emit tool_call），`json_repair` 仍是可行的 fallback 路径——这是 plan B

---

## 第 5 章 · 面试叙事要点

### Q: 你怎么做诊断 Agent 的收敛检测？

A: 我一开始走了弯路——基于 abductive completeness / Occam 这些概念一次性堆了 4 层机制（S1/S2/S3/S4）。跑 smoke 发现 4 个 case 里 2 个高分 case 所有机制都没触发，2 个低分 case 的真问题（DeepSeek 卡在 DSML 工具调用模式）这 4 层机制都解决不了。意识到问题是**理论先行**——没看真实失败模式就设计防御。

后来重做成 **case 驱动**：先跑极简 baseline（只有硬约束：12 轮 / 100k token / 300s），看 4 个 smoke case 的真实行为，找到具体失败模式，加最小机制，重跑对比。每加一行代码必须能指着一个具体 case 的具体轮次说「这里如果不加这个机制会失败」。

### Q: 为什么不用 Bayes 式假设置信度跟踪？

A: 我评估过这个方案，最终没采用。原因：让 LLM 每轮输出结构化 hypothesis list + Bayes 更新，在 DeepSeek/GPT-4o-mini 这一档模型上 confidence 严重 miscalibrated，且 `causal_path` 字段会退化为套话。set-cover 求解 + `is_causal_upstream` 判断也引入一堆新调参点。我让一个干净的评审 agent 评估过，他推荐简化版——保留行为锚定判据，只把 abductive completeness 作为事后门。后来我连简化版都砍了，因为 smoke 跑出来 S3 nudge 在 4 个 case 上 0 次触发——它解决的不是真问题。**这就是 case 驱动方法的价值：用证据砍掉听起来合理但实际没用的机制。**

### Q: 为什么不按"先收敛、后上下文"的顺序做？

A: 因为这两个维度天然耦合。比如收敛 nudge 引用「第 5 轮读到的 stack trace」，下一版上下文压缩把那条 ToolMessage 降级成 `[已归档]`，nudge 语境就断了。又比如上下文压缩触发依赖 `usage_ratio > 60%`，收敛早交付会减少 token 消耗，导致压缩机制在收敛好的 case 上根本不跑——你优化好的机制互相打架。

所以我改成**按 case 失败严重度排序，不按机制分类**：每次取最严重的失败模式来修，不管修的是收敛、上下文还是输出格式——修完跑全量 smoke，4 个 case 都不能退步才合并。一个失败模式可能需要同时改收敛和上下文，这没问题，反正每次只改一个失败模式。

### Q: REPORTING phase 为什么失败？

A: 设计假设是「禁工具 + 让 LLM 写 JSON = LLM 会写 JSON」。但 DeepSeek-V3 在对话历史有 tool_calls 时会持续输出 DSML 工具调用标记——这是模型层面的模式匹配，prompt 禁不掉。FE-020 的 trace 显示 agent 在 REPORTING phase 被禁工具后，连续 2 轮输出 DSML 标记想调 code_search / get_file_content，harness 的 `if "primary_category" in content` 检测不到这种失败，白白烧了 2 个 LLM call 才 break。**教训：不要假设 LLM 会服从 prompt 级别的模式切换，要看 trace 验证。**

### Q: 怎么知道一个机制是不是真有用？

A: 跑 smoke 前后对比 + 看 trace。具体三件事：
1. **那个目标 case 的分数涨了吗**——如果没涨，机制没起作用
2. **其他 case 退步了吗**——副作用检测
3. **trace 里机制真的触发了吗**——log 事件 + LLM 输出序列都要看

我之前加的 S3 missing-signal nudge，跑完 smoke 看 dump 脚本输出，发现 nudge 在 4 个 case 上 0 次触发——因为触发条件 `not budget_exhausted` 把所有真正需要它的 case 都挡在外面。如果只看分数不看 trace，我会以为机制生效了。

### Q: 你的 harness 全景是什么样？

A: 我把 harness 拆成 8 个维度：收敛/停止、输出格式/解析、上下文工程、Prompt 工程、工具行为、循环编排、预算/模型路由、失败恢复。对当前 4 个 smoke case，我预判输出格式和收敛是最大头（DSML 污染 + 早交付），上下文和 Prompt 其次，剩下四个各占一点点。

但我特意没按这个分类分阶段做——而是按 case 失败严重度做。每次改动我都会标注它落在哪个维度——回头看维度分布，发现比如收敛类 4 个、上下文类 3 个、输出格式类 2 个，**这个分布本身就是诊断 Agent harness 工程的特征画像**，比"我先做完收敛再做上下文"叙事有力得多。

---

## 第 6 章 · 工具脚本

| 脚本 | 用途 |
|---|---|
| `scripts/eval_agent.py --session <sid>` | 跑 4 个 smoke case 的端到端评估，写入 Langfuse session |
| `scripts/dump_session_scores.py <session_id>` | 拉一个 session 的 4 case 分数表 |
| `scripts/dump_trace_observations.py --session <sid> --bug <id>` | 看一个 trace 的 observation 列表 |
| `scripts/dump_trace_llm_responses.py --session <sid> --bug <id>` | 看一个 trace 的所有 LLM call 输入输出（最有用——能看 agent 每一轮在想什么） |
| `scripts/dump_obs_compact.py <trace_id>` | 紧凑版 observation dump |
| `scripts/dump_forced_call_flag.py <session_id>` | 看 Iteration 1 forced_final_json_call 在每个 case 是否真触发（区分「方差救活」vs「机制救活」） |

---

### Iteration 1: stop 后 forced final JSON call

- **日期**：2026-07-06
- **维度**：输出格式 / 解析 + 收敛（按 case 失败严重度而非维度分类做——这一改同时落在两个维度）
- **针对的 case**：BE-020 / FE-020 / FE-021（mode 1）+ BE-021 / CONFIG-020 / PERF-021（mode 2）——baseline 两次跑合计 5 个 disaster 全栽在这两种 failure mode 上
- **观测到的失败模式**：
  - **mode 1（跑到 cap + 末轮空 content）**：BE-020 / FE-020 / FE-021——agent 一直调工具到 `MAX_TOOL_CALLS=12`，末轮 LLM 把所有输出放进 `tool_calls` 字段，`content=""`。loop 因 `range(12)` 耗尽而 break，`parse_diagnosis_report` 拿到空 content → fallback 空报告（`primary_category=""` / `confidence=0.2`）
  - **mode 2（自然 stop + narrative 不带 JSON）**：BE-021 / CONFIG-020 / PERF-021——agent 自然停止调工具，但输出 1376 字叙事散文解释根因，不含 JSON 结构。`_extract_json_from_text` 找不到 JSON → fallback。`root_cause_accuracy` 仍是 0.95（judge 从 `root_cause=last_ai_content[:500]` 字段读到正确根因），但 `cat/file/fix/line` 全 0 因为没有结构化字段
  - 为什么没被现有机制拦住：baseline 砍光了所有收敛 / forced call / REPORTING phase 机制，loop 结束就直接 parse，parse 失败就给空报告
- **加的机制**：在 ReAct loop 结束后、`parse_diagnosis_report` 之前插入一次 forced final JSON call
  - **代码位置**：`doctor/src/graph/nodes/unified_agent.py`
    - 新增 `_FORCED_FINAL_JSON_SCHEMA_HINT` / `_FORCED_FINAL_INSTRUCTION_CAP` / `_FORCED_FINAL_INSTRUCTION_NARRATIVE` 三个 prompt 常量
    - 新增 `_last_ai_has_json(messages)` helper——判断 loop 末尾 AIMessage 是否已含可 parse 的 JSON
    - 新增 `_last_ai_is_natural_stop(messages)` helper——区分 mode 1 / mode 2 选不同 instruction
    - 新增 async `_forced_final_json_call(messages, llm, invoke_config, natural_stop, case_id)`——做一次额外 LLM call
    - `unified_agent_node` 在 loop 末尾插入触发分支
  - **逻辑简述**：
    1. loop 结束后查 `_last_ai_has_json(messages)`——已含 JSON 则跳过（不影响 baseline 11 个 healthy case）
    2. 不含 JSON 且 token 预算未爆时，按 `_last_ai_is_natural_stop` 选 instruction（mode 1 用「工具调用上限」instruction / mode 2 用「叙事性格式化」instruction）
    3. 把 `messages + [HumanMessage(instruction)]` 喂给**未 bind_tools 的** `llm`（不是 `llm_with_tools`——这是关键：从 API 层面拿掉 tool surface，DeepSeek 想 emit DSML 也 emit 不了）
    4. forced response append 进 messages，`parse_diagnosis_report` 自然 pick up 它作为新的 last AIMessage
  - **为什么是最小改动**：
    - 没有 phase 状态机、没有收敛检测、没有 nudge 注入、没有 hypothesis tracking——直接在 loop 末尾加一次 LLM call
    - 不改变 agent 在 loop 内的任何决策——只是「loop 后加一次格式化机会」
    - 单点触发条件（`not _last_ai_has_json`）+ 单点失败兜底（forced call 自己 raise/timeout → return None → 走原 fallback 路径），不引入新的状态变量
    - 与 v1 REPORTING phase 的关键差异：v1 在 loop **内**禁工具 + 注入 nudge，DeepSeek 仍持续输出 DSML 标记（API 层面 tools 还 bound，模型能继续 emit `tool_calls`，prompt 禁不掉）；本机制在 loop **外**做一次 call 且**完全不 bind tools**，从根本上拿掉了 DSML 触发面
- **单元测试**：
  - **测试文件**：`doctor/tests/graph/test_forced_final_json_call.py`（15 tests，全部通过）
  - **覆盖的场景**：
    - `_last_ai_has_json`：JSON object / markdown fence / 空 content / 纯叙事 / 无 AIMessage / 只看最后一条 AIMessage（6 cases）
    - `_last_ai_is_natural_stop`：无 tool_calls / 有 tool_calls / 无 AIMessage（3 cases）
    - `_forced_final_json_call`：append instruction 不 mutate input / mode 1 用 cap instruction / mode 2 用 narrative instruction / LLM 抛异常返回 None（4 cases）
    - 端到端 wire 进 `unified_agent_node`：mode 1 触发 forced call 并 parse 成功 / healthy case 不触发 forced call（2 cases，用 monkeypatch 替换 LLM 跑真实 ReAct loop）
- **smoke 重跑结果**：session=`smoke-v1-20260706-125927`（4 case，全部成功）
  - 4 case 分数对比表：

| case | iter1 overall | iter1 维度（root/cat/file/fix/proc） | baseline overall | Δ | forced_call 触发？ |
|---|---|---|---|---|---|
| BE-020 | **0.96** | 0.95 / 1.00 / 1.00 / 0.95 / 1.00 | 0.04（disaster, mode 1） | **+0.92** | **False**（agent 这次自然交付 JSON） |
| FE-020 | **0.79** | 0.85 / 0.50 / 1.00 / 0.45 / 1.00 | 0.04（disaster, mode 1） | **+0.75** | **True**（机制救活） |
| LOGIC-020 | **0.96** | 0.95 / 1.00 / 1.00 / 0.95 / 1.00 | 0.94（healthy） | +0.02 | False（gate 跳过） |
| PERF-020 | **0.87** | 0.95 / 1.00 / 1.00 / 0.95 / 1.00 | 0.83（healthy） | +0.04 | False（gate 跳过） |

  - **聚合**：smoke mean = **0.895**（baseline smoke mean ≈ 0.46），Δ=+0.43，远超 ±0.03 方差带 → 信号极强；disaster 数 = **0**（baseline 2 个）；零回归（两个 healthy case 都没退步）
  - **那个最差 case 改善了吗**：FE-020（0.04→0.79）是 forced call 直接救活的——trace 显示 LLM #12 自然 stop 输出 388 字 narrative（无 JSON），LLM #13 [LAST] 是 forced call 输出 1412 字**纯 JSON**（无 markdown 前缀），parse 成功，10 个字段全有
  - **其他 case 退步了吗**：没有。LOGIC-020 / PERF-020 两个 healthy case 都微涨（+0.02 / +0.04），`_last_ai_has_json` gate 正确跳过了 forced call——零回归设计验证通过
  - **关键诚实点（methodology §2.2「trace 里机制真的触发了吗」）**：
    - **BE-020 的救活是 LLM 方差，不是机制功劳**——trace `forced_final_json_call=False`，agent 这次在 cap 末轮自然交付了 2115 字 markdown+JSON（baseline 那次末轮是空 content）。这与 baseline ±0.03 方差带、disaster 归属不稳定的观察完全一致。如果只看分数不看 trace，会误以为机制救了 BE-020
    - **只有 FE-020 是机制的真证据**——forced call 真触发了，且把 mode 2（narrative 不带 JSON）转成了合法 JSON
    - **4 case 样本太小，BE-020 这种「方差救活」不可持续**——必须跑 train 8 case / 全量 15 case 看 forced call 触发率 + 真机制救活率才能下定论
  - **FE-020 残留问题**：cat=0.50 / fix=0.45 不满分——forced call 输出的 JSON 字段都有但**内容质量不够**（categories 选了 `frontend_crash + data` 但 grader 期望更准；fix_suggestion 是 null guard 而非 schema 修根因）。这是 instruction 的 schema hint / 引导问题，不是机制本身的交付问题——下一轮 iteration 可针对此调 instruction
- **结论**：**保留**。机制本身（forced final JSON call）在 FE-020 上证明有效，gate 在 3 个 healthy/naturally-delivered case 上正确跳过，零回归。但 4 case smoke 样本太小、BE-020 的救活是方差——**必须跑 train 8 case + 全量 15 case** 验证：(a) forced call 触发率是否稳定在合理区间（baseline disaster case 应触发）；(b) 真机制救活率（forced_call=True 且 parse 成功）是否高；(c) overall mean 是否 ≥0.75 且超过 baseline ±0.03 方差带；(d) disaster 数是否稳定 ≤2

#### 1.9 全量 15 case 验证（2026-07-06，session=`baseline-15case-v1-20260706-131440`）

> 注：run_name 字面是 `baseline-15case-v1`，但代码已是含 Iteration 1 forced call 的版本（git status 显示 `M doctor/src/graph/nodes/unified_agent.py`），所以这实质是 **iter1-full** 跑。

**15 case 分数 + forced_call flag**：

| bug_id | iter1 overall | iter1 维度（root/cat/file/fix/line/evid/conf/proc） | baseline v2 overall | Δ | forced_call | 归因 |
|---|---|---|---|---|---|---|
| BE-020 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.97/0.83 | 0.04 | +0.93 | **False** | 方差救活 |
| BE-021 | 0.98 | 1.00/1.00/1.00/0.98/1.00/0.85/0.98/1.00 | 0.31 | +0.67 | **False** | 方差救活 |
| BE-022 | 0.95 | 0.95/1.00/1.00/0.85/1.00/0.95/0.97/1.00 | 0.95 | 0 | False | healthy 稳定 |
| CASCADE-020 | 0.70 | 0.65/0.50/1.00/0.65/0.50/0.95/0.65/1.00 | 0.68 | +0.02 | False | 稳定 |
| **CONFIG-020** | **0.97** | 0.95/1.00/1.00/0.95/1.00/0.95/0.97/0.83 | 0.30 | **+0.67** | **True** | **机制救活** ✓ |
| DATA-020 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.95/1.00 | 0.96 | +0.01 | False | 稳定 |
| DATA-021 | 0.90 | 1.00/0.00/1.00/1.00/1.00/0.95/1.00/1.00 | 0.95 | -0.05 | False | 方差回归（cat=0.00，选错类） |
| **FE-020** | **0.73** | 0.65/0.50/1.00/0.55/1.00/0.95/0.70/1.00 | 0.04 | **+0.69** | **True** | **机制救活** ✓ |
| **FE-021** | **0.97** | 0.95/1.00/1.00/0.95/1.00/0.95/0.98/1.00 | 0.04 | **+0.93** | **True** | **机制救活** ✓ |
| LOGIC-020 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.97/1.00 | 0.94 | +0.03 | False | 稳定 |
| LOGIC-021 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.95/1.00 | 0.96 | +0.01 | **True** | 机制触发但本就 healthy，无回归 |
| LOGIC-022 | 0.94 | 0.95/1.00/1.00/0.95/1.00/0.70/0.97/1.00 | 0.94 | 0 | **True** | 机制触发但本就 healthy，无回归 |
| PERF-020 | 0.86 | 0.95/1.00/1.00/0.95/0.00/0.85/0.97/1.00 | 0.83 | +0.03 | False | 稳定（line=0，N+1 无单一行号） |
| **PERF-021** | **0.28** | 0.00/1.00/0.00/0.45/0.00/0.85/0.08/1.00 | 0.30 | -0.02 | **True** | **机制触发但救不了**（真诊断失败） |
| RACE-020 | 0.96 | 0.95/1.00/1.00/0.95/1.00/0.85/0.97/1.00 | 0.87 | +0.09 | False | 稳定 |

**聚合统计**：
- **overall mean = 0.874**（vs baseline v2 mean=0.666，**Δ=+0.208**，远超 ±0.03 方差带 → 信号极强）
- **disasters (overall<0.4) = 1**（PERF-021）——vs baseline v2 5 个，验收阈值「≤2」满足
- 各维度均值：root=0.853 / cat=0.867 / file=0.933 / fix=0.869 / line=0.833 / evid=0.907 / conf=0.872 / proc=0.978
- vs baseline v2 各维度：root 0.810→0.853 / cat 0.533→0.867 / file 0.667→0.933 / fix 0.607→0.869 / line 0.533→0.833 / evid 0.557→0.907 / conf 0.791→0.872 / proc 0.933→0.978——**所有维度都涨**，cat / file / fix / line 涨幅最大（这些正是需要结构化 JSON 才能拿分的维度）

**forced_call 触发分布（6/15 = 40%）**：
- **机制救活 3 个 disaster**：CONFIG-020（0.30→0.97）/ FE-020（0.04→0.73）/ FE-021（0.04→0.97）——forced_call=True 且从 disaster → healthy，**这是机制的真证据**
- **方差救活 2 个 disaster**：BE-020（0.04→0.97）/ BE-021（0.31→0.98）——forced_call=False，agent 这次恰好自然交付 JSON，与 baseline v2 的 BE-021 0.31→0.97 出灾难区是同种方差现象
- **机制触发但救不了 1 个**：PERF-021——forced_call=True，forced call (LLM #13) 输出 1184 字干净 JSON（10 字段全有，parse 成功），**但诊断内容错**：agent 把「项目列表慢」误读成「任务列表 N+1」（PERF-020 的 pattern），affected_file 错指 `tasks.py` 而非 project 路径。这是「推理失败」而非「交付失败」，Iteration 1 的设计点不在这
- **机制在 healthy case 触发 2 个，无回归**：LOGIC-021（0.96→0.97）/ LOGIC-022（0.94→0.94）——agent 这次恰好 narrative stop，forced call 把 narrative 转 JSON，分数没掉。证明 gate 是「last AI 有没有 JSON」而非「case 历史是否 healthy」——这是正确设计：防止方差导致的临时 narrative stop 让 healthy case 退步
- **gate 正确跳过 9 个**：BE-020 / BE-021 / BE-022 / CASCADE-020 / DATA-020 / DATA-021 / LOGIC-020 / PERF-020 / RACE-020——agent 已交付 JSON，零误判

**新失败模式定位（PERF-021，看 trace LLM call 序列验证过）**：
- **症状误读 + 锁死错误路径**：
  - LLM #4：agent 看到 `GET /api/projects/` 只有 2 个 SQL 查询（24ms+52ms），"并不慢"——这是关键岔路口
  - LLM #4：agent **误判**——"可能指的是进入某个项目后看到任务列表变慢"，把症状从「项目列表」偷换成「任务列表」
  - LLM #5：在 `GET /api/projects/{id}/tasks` 找到 N+1（50 次 comments 查询）——这是 **PERF-020 的 bug pattern**，不是 PERF-021
  - LLM #6-12：agent 在错误路径上越走越深，LLM #12 甚至注意到矛盾（代码有 `selectinload` 但 trace 显示 N+1）却解释成「selectinload 未生效或已被 Bug Factory 污染」而非「看错了 endpoint」
  - LLM #13 forced call：把错误诊断格式化成漂亮 JSON——机制救不了错诊断
- **根因**：agent 推理能力在「症状 → 路径」映射这一步出错，不是工具调用不够、不是 JSON 交付不出。属于 Prompt / 推理维度，不是输出格式维度
- **PERF-021 注入日志佐证**：注入的是 `app.models.user` / `app.schemas.project`（project 路径），agent 错指 `tasks.py` 是错的

**最终结论（Iteration 1）**：
- **保留机制**。验收阈值全部满足：mean=0.874 ≥0.75 ✓，disasters=1 ≤2 ✓，Δ=+0.208 远超 ±0.03 方差带 ✓
- **机制真证据**：3 个 disaster 被 forced call 直接救活（CONFIG-020 / FE-020 / FE-021），6 个维度涨幅都超过方差带
- **零回归**：9 个 gate-skipped case 全部不退步；2 个 healthy-but-forced-triggered case 也不退步
- **诚实归因**：BE-020 / BE-021 的救活是 LLM 方差（forced_call=False），不是机制功劳。如果只看分数不看 trace flag，会高估机制效果——这正是 methodology §2.2「trace 里机制真的触发了吗」的纪律价值
- **新失败模式（PERF-021）留给 Iteration 2+**：「症状误读 + 锁死错误路径」——属于 Prompt / 推理维度，forced call 救不了。这是 case 驱动方法的下一轮起点

**面试叙事点（全量补充）**：
- **方差带纪律的实战价值**：baseline v2 已经建立 ±0.03 方差带 + disaster 归属不稳定的认知。全量跑出来 BE-020 / BE-021 forced_call=False 但分数飙高——只有靠 trace flag 才能区分「方差救活」vs「机制救活」。**没有方差带认知就会把 5 个 disaster 全归功给机制**，归因错就会误判机制的真实效果边界（以为它能救 PERF-021，其实救不了）
- **机制的「能力边界」由 case 揭示**：PERF-021 是 forced call 触发了但救不了的 case——它界定了 Iteration 1 的能力边界：**机制只解决「交付」不解决「推理」**。这个边界不是设计时画出来的，是跑完全量才看见的。这就是 case 驱动方法 vs 概念驱动的根本差异
- **gate 设计的「方差防护」副作用**：LOGIC-021 / LOGIC-022 本就是 healthy case，但这一轮 agent 恰好 narrative stop（mode 2），forced call 触发把 narrative 转 JSON——分数没掉反而 LOGIC-021 微涨。这说明 gate 不是简单的「省 cost」，还有「防止方差导致的临时 narrative stop 让 healthy case 退步」的方差防护作用——这是设计时没预想到的红利
- **面试叙事点**：
  - **「拿掉 tool surface」比「prompt 禁工具」可靠**：v1 REPORTING phase 的失败教训是 prompt 级模式切换打不过 DeepSeek 在 tool-call history 下的 DSML pattern matching。Iteration 1 从 API 层面解决——`llm.ainvoke`（不 `bind_tools`）让模型在 response 里**根本没有 `tool_calls` 字段可填**，DSML 也就无从触发。FE-020 的 forced call（LLM #13）输出纯 JSON 无任何 DSML 痕迹，直接验证了这点
  - **单机制覆盖两种 failure mode**：mode 1 / mode 2 看似不同（一个是「停不下来」、一个是「停了但不格式化」），但根因都是「loop 末尾没有 content-only 的输出机会」。一次 forced call 同时救两种——这是「按 case 失败严重度做、不按维度分类做」的方法论红利（§2.4）
  - **健康 case 零回归设计**：`_last_ai_has_json` gate 确保 healthy case 不会被多塞一次 LLM call——既省 cost 又避免给已交付的 case 增加方差。smoke 4 case 里 3 个 case（BE-020 / LOGIC-020 / PERF-020）gate 正确跳过，验证通过
  - **可观测性 + 诚实归因**：forced call 触发与否写入 Langfuse `output_data.forced_final_json_call` 字段。这次 smoke 跑出来 BE-020 分数飙到 0.96，但 trace flag 显示 `forced_call=False`——是 LLM 方差救的不是机制救的。**「看分数不看 trace」会归因错**，methodology §2.2 的第三件事「trace 里机制真的触发了吗」就是防这个坑
  - **case 驱动方法对「方差 vs 信号」的纪律**：baseline 复跑已经建立 ±0.03 方差带 + disaster 归属不稳定的认知，所以 smoke 4 case 的 +0.43 不能直接当机制功劳——必须拆开看 forced_call flag 才能区分「方差救活」（BE-020）和「机制救活」（FE-020）。这是「先建立方差带再做 iteration」的工程纪律红利

#### 1.10 泄漏清理 + strict=False 修复（2026-07-06，session=`baseline-15case-v1-20260706-154516`）

> Iteration 1 全量验证后，发现测试 fixture 存在信息泄漏（demo-app 源码 / seed data / 工具定义中暴露 "Bug Factory" / "N+1" / "Doctor agent" 等实验框架关键词，可能 bias agent 诊断）。清理泄漏后重跑全量 15 case。

**泄漏清理（10 文件）**：
- `tasks.py`：移除 docstring 中的 Bug Factory / N+1 / healthy baseline 提示（-8 行）
- `seed.py`：移除 TODO 标题与评论中的 N+1 优化提示
- `observability.py` / frontend observability / error-reporter：移除 Doctor agent / Doctor ingest 等实验框架暴露
- `code_search.py`：`bug_recipe` 标签 → `tooling`
- `cascade_020` recipe：`[CASCADE]` 日志前缀 → `[RETRY]`（recipe 注入的代码里直接标注了 bug 类型，agent 看 log 就知道答案）
- 7 个 recipe diff_patch 行号同步（tasks.py 清理后上移 8 行）

**附带修复：`json.loads(strict=False)`**：
- 初始诊断：forced call 产出的 JSON 含字面换行（pretty-printed），`strict=True` 拒绝 → 加 `strict=False`
- **后续验证发现此诊断不完全**：3 个 disaster 的 JSON 确实有字面换行，但 `\n` 都是 escaped `\n`（backslash-n），不是字面控制字符。真正的 parse 失败原因是**未转义双引号**（见下）。`strict=False` 仍保留（处理字面换行的 subset），但单独不够。

**15 case 分数**：

| bug_id | overall | root | cat | file | fix | line | evid | conf | proc | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| BE-020 | 0.97 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.92 | 0.97 | 1.00 | |
| BE-021 | 0.97 | 1.00 | 1.00 | 1.00 | 0.95 | 1.00 | 0.85 | 0.95 | 1.00 | |
| BE-022 | 0.95 | 0.95 | 1.00 | 1.00 | 0.85 | 1.00 | 0.95 | 0.97 | 1.00 | |
| CASCADE-020 | 0.79 | 0.65 | 1.00 | 1.00 | 0.85 | 0.50 | 0.95 | 0.67 | 1.00 | 根因不完整 |
| **CONFIG-020** | **0.27** | 0.85 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.35 | 0.83 | **JSON parse 失败** |
| **DATA-020** | **0.33** | 0.95 | 0.00 | 0.00 | 0.00 | 0.00 | 0.30 | 0.25 | 1.00 | **JSON parse 失败** |
| DATA-021 | 0.95 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.80 | 0.97 | 0.83 | |
| FE-020 | 0.71 | 0.65 | 0.50 | 1.00 | 0.45 | 1.00 | 0.95 | 0.70 | 0.83 | fix 策略选错 |
| FE-021 | 0.43 | 0.30 | 1.00 | 0.00 | 0.72 | 0.00 | 0.72 | 0.42 | 1.00 | 根因层级错配 |
| LOGIC-020 | 0.97 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.95 | 0.97 | 1.00 | |
| LOGIC-021 | 0.96 | 0.95 | 1.00 | 1.00 | 0.95 | 1.00 | 0.85 | 0.97 | 1.00 | |
| **LOGIC-022** | **0.33** | 0.95 | 0.00 | 0.00 | 0.00 | 0.00 | 0.30 | 0.25 | 1.00 | **JSON parse 失败** |
| PERF-020 | 0.86 | 0.95 | 1.00 | 1.00 | 0.95 | 0.00 | 0.85 | 0.97 | 1.00 | line=0 |
| PERF-021 | 0.92 | 0.95 | 1.00 | 1.00 | 0.95 | 0.50 | 0.95 | 0.97 | 1.00 | 泄漏清理后恢复 |
| RACE-020 | 0.92 | 0.95 | 1.00 | 1.00 | 0.95 | 0.50 | 0.95 | 0.97 | 1.00 | |

- **聚合统计**：overall mean=**0.755**，min=0.27，max=0.97
- **3 个 disaster**（overall<0.4）：CONFIG-020 / DATA-020 / LOGIC-022——全部 `notes="JSON 解析失败，使用原始输出作为 root_cause"`
- **vs Iteration 1 full（mean=0.874）**：Δ=−0.119，远超 ±0.03 方差带。下降主因是 3 个 case 从 0.97/0.97/0.94 跌到 0.27/0.33/0.33——iteration 1 full 时 LLM 恰好没产出 unescaped quotes，这轮恰好产出了。**这恰好证明了 JSON 交付是一个高方差事件**，forced call 确保 LLM 产出 JSON，但不保证 JSON 语法正确。

**3 个 disaster 根因深挖（`debug_json_parse.py` 实测）**：

用 `json.loads(strict=False)` 逐个测 3 个 disaster 的 forced call 原始输出，全部报 `Expecting ',' delimiter`——不是字面控制字符问题（`strict=False` 无效），而是 **LLM 在 JSON 字符串值中放了未转义的 ASCII 双引号**：

| Case | 出错位置 | 未转义内容 |
|---|---|---|
| CONFIG-020 | fix_suggestion 字段 | `表现为"登录成功后马上就掉登录态"。` |
| DATA-020 | root_cause 字段 | `与用户期望的"最新在前"相反` + evidence_chain 里也有 |
| LOGIC-022 | fix_suggestion 字段 | `用户反馈"其它字段能改"完全吻合` |

LLM 在中文叙事中用 `"..."` 引述短语是常见行为，但忘了在 JSON 字符串值里 escape 成 `\"...\"`。这是 JSON 语法错误，`strict=False` 解决不了。

---

### Iteration 2: structured output via `with_structured_output(method="function_calling")` + Langfuse observability fix

> **重要诚实点**：Iteration 2 计划阶段写的是「`json_repair.repair_json()` fallback」（见第 4 章 graveyard）。
> 实施时换成了**完全不同的方案**——把 forced call 从「让 LLM 写 free-text JSON 再 parse」改成
> 「`with_structured_output(method="function_calling")` 在 API 层面用 tool calling 强制 schema」。
> 换方案的理由是：`json_repair` 只能在 parse 失败后**修补**，治标；而 `with_structured_output`
> 在 API 层面让模型 emit 一个 tool_call（args 由 schema 校验），从源头消除未转义引号问题，治本。
> 这是 case 驱动方法里允许的「实施时发现更优解」——但要在文档里诚实记录，不能让叙事伪装成「按计划执行」。

- **日期**：2026-07-07（实施 + 全量验证完成）
- **维度**：输出格式 / 解析（harness 层处理 LLM 输出的健壮性）+ 可观测性（让机制在 Langfuse 里可见）
- **针对的 case**：CONFIG-020 (0.27) / DATA-020 (0.33) / LOGIC-022 (0.33)——3 个 disaster，同一失败模式
- **观测到的失败模式**：
  - **第几轮失败**：forced call（LLM #13）产出了结构完整、内容正确的 JSON，但 `_extract_json_from_text` → `json.loads` 解析失败
  - **失败行为**：LLM 在 JSON 字符串值中用未转义的 ASCII 双引号 `"..."` 引述中文短语（如 `表现为"登录成功后马上就掉登录态"`），JSON 解析器把第一个 `"` 当成字符串结束符 → `Expecting ',' delimiter`
  - **特征**：`root_cause_accuracy` 高（0.85-0.95）——judge 从 fallback 的 `root_cause=last_ai_content[:500]` 读到了正确根因；但 `cat/file/fix/line/evid` 全 0——结构化字段全部丢失
  - **为什么没被现有机制拦住**：Iteration 1 的 forced call 机制工作正常（LLM 确实产出 JSON），`strict=False` 只处理字面控制字符，不处理未转义双引号——这是 JSON 语法层面的错误
- **为什么是 Iteration 2 而非 Iteration 1 bugfix**：
  - Iteration 1 = "确保 agent 输出 JSON"（forced call 机制）——已完成，LLM 确实产出 JSON
  - JSON parse 失败是另一个层的问题：harness 如何 robust 地处理 LLM 产出的**不完美** JSON
  - 这是 harness 层的输出处理能力，不是 agent 的诊断推理能力——属于不同维度，值得独立迭代
- **加的机制（实施版本，与计划不同）**：
  - **代码位置**：`doctor/src/graph/nodes/diagnosis_agent/forced_call.py` 的 `_forced_final_json_call`
  - **逻辑简述**：把 forced call 从「un-bound LLM + prompt 要求输出 JSON 文本」改成
    「un-bound LLM + `with_structured_output(ForcedDiagnosisReport, method="function_calling", include_raw=True)`」。
    模型 emit 一个 tool_call（content="" + tool_calls=[{ForcedDiagnosisReport, args=...}]），
    LangChain 从 tool_call args 解析成 `ForcedDiagnosisReport` Pydantic 对象，再 `model_dump_json()`
    序列化成合法 JSON 字符串包进合成 `AIMessage`——`parse_diagnosis_report` 拿到的就是
    **schema-validated + pydantic-escaped** 的 JSON，未转义引号问题从源头消除。
  - **`method="function_calling"` 是必需的，不是默认**：`ChatOpenAI.with_structured_output` 默认
    `method="json_schema"`（走 OpenAI Structured Output API / `response_format={"type":"json_schema",...}`）。
    DeepSeek（`deepseek-v4-flash`）**不支持** `response_format=json_schema`——返回 400
    `'This response_format type is unavailable now'`。`json_mode` 也 400（要求 prompt 含 'json' 字样，
    且不强制 schema）。`function_calling` 走 tool calling，DeepSeek 支持（ReAct loop 一直在用）。
    用 `scripts/debug_structured_output_methods.py` 实测过三种 method，结论写在该脚本注释里。
  - **为什么不是 `json_repair`（计划方案）**：`json_repair` 是事后修补，需要 LLM 先产出错的 JSON 再 fix。
    `with_structured_output(method="function_calling")` 是事前预防——schema 在 API 层强制，LLM 不需要
    自己处理 JSON 字符串转义（pydantic 接管）。治本优于治标，且不需要新依赖。
  - **为什么仍是最小改动**：
    - 不改 forced call 的触发 gate / 不改 ReAct loop / 不改 system prompt
    - 只换 forced call **内部**的 LLM 调用方式（free-text JSON → structured output tool_call）
    - 下游 `parse_diagnosis_report` 不变——合成 AIMessage 的 content 仍是 JSON 字符串
    - `ForcedDiagnosisReport` Pydantic schema 是 `DiagnosisReport` 的 slim 版（去掉 `early_stopped` /
      `notes` 这些 harness-controlled 字段，防止 LLM 乱填）
- **Langfuse 可观测性修复（同 iteration 一起做）**：
  - **问题**：`with_structured_output(method="function_calling")` 的 parsed Pydantic 对象对 Langfuse 完全
    不可见——Langfuse 的 `on_llm_end` callback 只拿到 raw model response（`content="" + tool_calls=[...]`），
    而 LangChain 把 tool_call args 解析成 Pydantic 是在 callback fire **之后**发生的。第一次跑 3 case
    验证时，`debug_structured_forced_call.py` 看到 forced call 的 LLM #13 output 是 `{"content": ""}`——
    机制实际工作了（`forced_call_diag.log` 有 parsed 对象），但 trace 看不见，导致误判「结果不太对」。
  - **修复**：两层
    1. **`on_llm_end` 捕获 `tool_calls`**（`src/observability/langfuse_tracing.py`）：新增
       `_extract_tool_calls(response)`，让 generation observation 的 output 从 `{"content": "..."}`
       升级成 `{"content": "...", "tool_calls": [{name, args, id}, ...]}`。这是**通用 fix**——
       ReAct loop 里 agent 决定调工具的那一轮（content="" + tool_calls=[{search_observability, ...}]）
       之前在 Langfuse 里也是空 content，现在 tool 决策完全可见。
    2. **`record_structured_output` 显式 SPAN**（`src/observability/langfuse_tracing.py` 新方法）：
       在 `_forced_final_json_call` 的成功 / parsed=None / exception 三条路径里显式调一次
       `langfuse_handler.record_structured_output(schema_name="ForcedDiagnosisReport", parsed=..., error=...)`，
       把 parsed Pydantic 对象作为 `structured_output_ForcedDiagnosisReport` SPAN observation 写进 trace。
       所有 handler 调用包在 `contextlib.suppress(Exception)` 里——Langfuse 故障绝不阻断诊断路径。
  - **验证脚本**：新增 `scripts/dump_structured_output_spans.py`——dump 一个 session 里所有
    `structured_output_*` SPAN，验证 record 是否落地。
- **单元测试**：
  - **测试文件**：`doctor/tests/graph/test_forced_final_json_call.py`（22 tests，全部通过）
  - **覆盖的场景**：
    - 原 15 个测试（Iteration 1）保留：`_last_ai_has_json` / `_last_ai_is_natural_stop` /
      `_forced_final_json_call` 的 mode 1/mode 2/异常/parsed=None/JSON 字段完整性
    - **新增 5 个观测性测试** `TestStructuredOutputObservability`：
      - 成功路径调 `record_structured_output`，验证 schema_name / parsed dict / case_id
      - parsed=None 路径调，验证 error 字段
      - LLM 异常路径调，验证 error 含异常类型和消息
      - 默认 `langfuse_handler=None` 不 raise（向后兼容）
      - handler 自己 raise 时被 suppress，forced call 仍成功返回
- **3 case 验证**（session=`iter2-structured-v2-20260707-142904`）：
  - 3 case 分数：CONFIG-020=0.97 / DATA-020=0.94 / LOGIC-022=0.94，mean=0.952，0 disaster
  - forced_call 触发 2/3（DATA-020 + LOGIC-022），CONFIG-020 是方差救活
  - DATA-020 是机制真证据：LLM #8 natural-stop 输出 markdown+JSON 但含未转义引号
    `与用户期望的"最新在前"相反` → gate 正确检测 parse 失败 → 触发 forced call →
    structured output 产出干净 args → 序列化成合法 JSON → 0.94 分
  - 观测性修复两层都验证：LLM #9 [forced call] 的 generation output 现在含完整 tool_calls；
    两个 forced_call=True trace 都有 `structured_output_ForcedDiagnosisReport` SPAN，parsed 对象可见
- **全量 15 case 验证**（session=`baseline-15case-v2-20260707-144018`）：
  - **15 case 分数 + forced_call flag**：

| bug_id | iter2 overall | iter2 维度（root/cat/file/fix/line/evid/conf/proc） | iter1+strict overall | Δ | forced_call | json_ok | structured_output SPAN | 归因 |
|---|---|---|---|---|---|---|---|---|
| BE-020 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.97/0.83 | 0.97 | 0 | False | yes | 0 | healthy 稳定 |
| BE-021 | 0.98 | 0.98/1.00/1.00/0.95/1.00/0.98/1.00/1.00 | 0.97 | +0.01 | False | yes | 0 | healthy 稳定 |
| BE-022 | 0.95 | 0.95/1.00/1.00/0.85/1.00/0.95/0.97/1.00 | 0.95 | 0 | False | yes | 0 | healthy 稳定 |
| CASCADE-020 | 0.79 | 0.65/1.00/1.00/0.85/0.50/0.95/0.67/1.00 | 0.79 | 0 | False | yes | 0 | 根因不完整（推理维度） |
| **CONFIG-020** | **0.97** | 0.95/1.00/1.00/0.95/1.00/0.95/0.97/0.83 | **0.27** | **+0.70** | **True** | yes | **1** | **机制救活** ✓ |
| **DATA-020** | 0.96 | 0.95/1.00/1.00/0.95/1.00/0.85/0.97/1.00 | **0.33** | **+0.63** | False | yes | 0 | 方差救活（这次自然 escape 了） |
| DATA-021 | 0.90 | 1.00/0.00/1.00/1.00/1.00/0.95/1.00/1.00 | 0.95 | -0.05 | False | yes | 0 | 方差回归（cat=0.00 选错类） |
| **FE-020** | 0.66 | 0.85/1.00/0.00/0.85/0.00/0.85/0.90/1.00 | 0.71 | -0.05 | **True** | yes | **1** | 机制交付成功，file/line 诊断错（推理失败） |
| **FE-021** | **0.97** | 0.95/1.00/1.00/0.95/1.00/0.95/1.00/1.00 | **0.43** | **+0.54** | **True** | yes | **1** | **机制救活** ✓ |
| LOGIC-020 | 0.96 | 0.95/1.00/1.00/0.95/1.00/0.85/0.97/1.00 | 0.97 | -0.01 | False | yes | 0 | healthy 稳定 |
| LOGIC-021 | 0.97 | 0.95/1.00/1.00/0.95/1.00/0.95/0.95/1.00 | 0.96 | +0.01 | False | yes | 0 | healthy 稳定 |
| **LOGIC-022** | 0.94 | 0.95/1.00/1.00/0.95/1.00/0.70/0.97/0.83 | **0.33** | **+0.61** | False | yes | 0 | 方差救活（这次自然 escape 了） |
| PERF-020 | 0.84 | 0.95/1.00/1.00/0.95/0.00/0.70/0.97/1.00 | 0.86 | -0.02 | False | yes | 0 | line=0（N+1 无单一行号） |
| **PERF-021** | **0.92** | 0.95/1.00/1.00/0.95/0.50/0.95/0.97/1.00 | 0.92 | 0 | **True** | yes | **1** | **机制救活** ✓（iter1-full 时是 0.28 推理失败，这次推理对了） |
| RACE-020 | 0.86 | 0.95/1.00/1.00/0.65/0.50/0.95/0.97/1.00 | 0.92 | -0.06 | False | yes | 0 | fix=0.65（乐观锁建议弱） |

  - **聚合统计**：
    - **overall mean = 0.909**（vs iter1+strict 0.755，**Δ=+0.154**，远超 ±0.03 方差带 → 信号极强）
    - **disasters (overall<0.4) = 0**——vs iter1+strict 3 个，验收阈值「≤1」超额满足
    - 各维度均值：root=0.929 / cat=0.933 / file=0.933 / fix=0.913 / line=0.767 / evid=0.899 / conf=0.950 / proc=0.967
    - **15/15 `json_ok=yes`——零 JSON parse 失败**（vs iter1+strict 的 3 个 unescaped-quote disaster）
  - **forced_call 触发分布（5/15 = 33%）**：
    - **机制救活 3 个 disaster**：CONFIG-020（0.27→0.97）/ FE-021（0.43→0.97）/ PERF-021（0.28→0.92）——
      forced_call=True 且从 disaster → healthy，**这是机制的真证据**
    - **机制触发但救不了 1 个**：FE-020（0.71→0.66）——forced_call=True，structured output 产出了
      干净 JSON（json_ok=yes），但 file=0/line=0 因为 agent 把 bug 归因到 backend `tasks.py`
      而 grader 期望前端文件。**机制交付成功，诊断推理错**——属于 Prompt/推理维度，不是输出格式
    - **方差救活 2 个**：DATA-020（0.33→0.96）/ LOGIC-022（0.33→0.94）——forced_call=False，
      agent 这次恰好自然 escape 了引号，与 iter1-full 的 BE-020/BE-021 同现象
    - **gate 正确跳过 10 个**：BE-020 / BE-021 / BE-022 / CASCADE-020 / DATA-021 / LOGIC-020 /
      LOGIC-021 / PERF-020 / RACE-020——agent 已交付可 parse JSON，零误判
  - **观测性修复 1:1 对应验证**：5 个 forced_call=True trace 全部有 1 个
    `structured_output_ForcedDiagnosisReport` SPAN（parsed 对象可见），10 个 forced_call=False trace
    全部 0 SPAN——gate 行为与 record 落地完全一致
- **结论**：**保留**。验收阈值全部满足：
  - mean=0.909 ≥0.85 ✓，disasters=0 ≤1 ✓，Δ=+0.154 ≥+0.10 ✓
  - forced_call=True case 的 JSON parse 成功率 5/5=100% ✓
  - healthy case 零回归（10 个 gate-skipped case 都在 0.79-0.98，方差带内）✓
  - Langfuse 可观测性修复让机制完全可见（5/5 SPAN 落地）✓
- **诚实归因**：
  - DATA-020 / LOGIC-022 这次的救活是 LLM 方差（forced_call=False），不是机制功劳——与 iter1-full 的
    BE-020/BE-021 完全同现象。如果要稳定看 forced_call 触发率，需要再跑 1-2 次全量
  - FE-020 (0.66) 的低分不是机制问题——`json_ok=yes` 说明 structured output 交付成功，问题是
    agent 推理把前端 crash 归因到后端 `tasks.py`（grader 期望前端文件），属于 Prompt/推理维度
- **面试叙事点**：
  - **「实施时发现更优解」的诚实记录**：计划是 `json_repair`（事后修补），实施时换成
    `with_structured_output(method="function_calling")`（事前预防）。两者解决同一失败模式但治本治标不同。
    case 驱动方法不排斥实施时换方案，但必须文档记录——不能让事后叙事伪装成「按计划执行」
  - **`method="function_calling"` 不是默认值，是 DeepSeek 兼容性必需**：`ChatOpenAI.with_structured_output`
    默认 `method="json_schema"`（OpenAI Structured Output API），DeepSeek 返回 400
    `'This response_format type is unavailable now'`。`scripts/debug_structured_output_methods.py`
    实测三种 method 留作证据。**这是「不要假设默认值跨 provider 通用」的工程教训**
  - **可观测性修复是 mechanism design 的一部分，不是事后补丁**：第一版 `with_structured_output` 实施
    完跑 3 case，trace 里 forced call 长得像 0-char 输出，差点误判「机制不工作」。拉 `forced_call_diag.log`
    才看到 parsed 对象其实产出了。**教训：换 LLM 调用方式时必须同步考虑 callback handler 看不看见
    新路径的产物**——`with_structured_output` 的 parsed Pydantic 在 callback fire 之后才生成，
    必须 explicit record 才能在 trace 里可见
  - **机制的能力边界由 case 揭示**：FE-020 (0.66) 是 forced_call=True 但救不了的 case——
    `json_ok=yes` 证明 structured output 交付成功，但 file/line 诊断错。**机制只解决「交付」
    不解决「推理」**——与 iter1-full 的 PERF-021「forced call 救不了推理失败」同理。
    这个边界不是设计时画出来的，是跑完全量 + 看 parsed 对象内容才看见的
  - **JSON 交付仍是高方差事件**：DATA-020 / LOGIC-022 两次跑分别被机制救活和方差救活——
    证明 agent 是否 escape 引号是 LLM 方差，不是 case 固有属性。`with_structured_output` 的价值是
    **消除这个方差**：不论 LLM 自然输出有没有 escape，gate 触发时 structured output 都产出合法 JSON

---

## 下一次 iteration kickoff 备忘

> 每次开新会话窗口时，让 AI 先读这份文档，再从下面这段继续。

```
当前状态：Iteration 2 已完成（structured output via with_structured_output + Langfuse 可观测性修复）。
  Iteration 2 机制：把 forced call 从「un-bound LLM + prompt 要求输出 free-text JSON」改成
    「un-bound LLM + with_structured_output(ForcedDiagnosisReport, method="function_calling", include_raw=True)」。
    模型 emit 一个 tool_call（content="" + tool_calls=[{ForcedDiagnosisReport, args=...}]），
    LangChain 从 args 解析成 Pydantic 对象，再 model_dump_json() 序列化成合法 JSON 字符串
    包进合成 AIMessage——parse_diagnosis_report 拿到的是 schema-validated + pydantic-escaped
    的 JSON，未转义引号问题从源头消除。
  method="function_calling" 是 DeepSeek 兼容性必需（不是默认值）：默认 "json_schema" 走 OpenAI
    Structured Output API，DeepSeek 返回 400 'This response_format type is unavailable now'。
    用 scripts/debug_structured_output_methods.py 实测过三种 method。
  Langfuse 可观测性修复（两层）：
    1. on_llm_end 现在捕获 tool_calls（_extract_tool_calls）——所有 tool-call AIMessage 在
       Langfuse generation output 里不再只是 {"content": ""}，而是 {"content": "...", "tool_calls": [...]}
    2. record_structured_output 显式 SPAN——_forced_final_json_call 三条路径（成功/parsed=None/异常）
       都调一次 langfuse_handler.record_structured_output(...)，把 parsed Pydantic 对象作为
       structured_output_ForcedDiagnosisReport SPAN observation 写进 trace。所有 handler 调用
       包在 contextlib.suppress(Exception) 里，Langfuse 故障不阻断诊断路径。
  全量结果（session=baseline-15case-v2-20260707-144018，Iteration 2 完整版）：
    mean=0.909（vs iter1+strict 0.755，Δ=+0.154）
    disasters=0（vs iter1+strict 3 个 unescaped-quote disaster）
    15/15 json_ok=yes——零 JSON parse 失败
    forced_call 触发 5/15：CONFIG-020/FE-020/FE-021/PERF-021 forced_call=True，全部 json_ok=yes
      机制救活 3 个 disaster：CONFIG-020 (0.27→0.97) / FE-021 (0.43→0.97) / PERF-021 (0.28→0.92)
      机制触发但救不了 1 个：FE-020 (0.66) ——json_ok=yes 但 file/line 诊断错（推理失败非交付失败）
      方差救活 2 个：DATA-020 / LOGIC-022 ——forced_call=False，这次 agent 自然 escape 了引号
    structured_output SPAN 1:1 对应：5 个 forced_call=True trace 都有 SPAN，10 个 False 都没有

  测试：tests/graph/test_forced_final_json_call.py 22 passed（15 原有 + 5 新增观测性 + 2 集成）
  工具脚本新增：
    scripts/debug_structured_output_methods.py —— 实测 DeepSeek 三种 with_structured_output method
    scripts/dump_structured_output_spans.py —— dump session 里所有 structured_output_* SPAN

下一步：Iteration 3 候选，全是推理维度（Iteration 1+2 已把交付维度吃透）：
  FE-020 (0.66) ——file/line 归因分歧（forced_call 交付成功但 agent 把前端 crash 归因到后端 tasks.py）
  CASCADE-020 (0.79) ——根因不完整（L4 级联 case，部分识别 N+1+retry storm）
  DATA-021 (0.90) ——cat=0.00 类别选错
  RACE-020 (0.86) ——fix=0.65 乐观锁建议弱
  这些是真实诊断推理失败，属于 Prompt / 推理维度，留给后续 iteration。

  Iteration 3 节奏（待启动）：
    # 先看 FE-020 trace 找具体失败轮次（agent 在哪一步把前端 crash 归因到后端？）
    cd D:\Work\LearnAI\DiagDoctor\doctor
    uv run python scripts/dump_trace_llm_responses.py --session baseline-15case-v2-20260707-144018 --bug FE-020
    uv run python scripts/debug_structured_forced_call.py baseline-15case-v2-20260707-144018 FE-020

    # 设计最小机制针对那个失败轮次（可能是 system prompt 加"前后端 blame 倾向"指引，
    # 也可能是 inspect_frontend_error 工具结果格式调整），实施 + 单元测试 + 全量 15 case 重跑

命令（Iteration 3 节奏）：
  cd D:\Work\LearnAI\DiagDoctor
  uv run python scripts/eval_agent.py --run-name iter3-foo
  uv run python scripts/dump_session_scores.py iter3-foo-<ts>
  uv run python scripts/dump_forced_call_flag.py iter3-foo-<ts>
  uv run python scripts/dump_structured_output_spans.py iter3-foo-<ts>

注意：run_name 现在会自动补 timestamp 后缀（避免 Langfuse session 前缀聚合歧义）。
  显式传 --run-name iter3-foo → 实际 run_name = iter3-foo-20260705-2103
  已带 YYYYMMDD-HHMMSS 后缀的不再重复补。
```

---

### Iteration 3: Framework migration（`langchain.create_agent` + 5 middleware，替换手写 ReAct loop）

> **叙事定位**：这不是一个"涨分"迭代，是一个**工程重构迭代**——把 Iteration 0–2 在手写 `while` 循环里积累的 5 个机制（forced final JSON call / tool 去重 / tool 结果截断 / budget guard / Langfuse tracing）迁移到 `langchain.create_agent` + middleware 框架。验收标准是 **parity（与 Iteration 2 全量 baseline 持平）**，不是涨分。parity 的意义：证明"手写循环能做的精确控制，框架的 6 个钩子也能做到"，从而把项目叙事从"我手写了一个 agent loop"升级为"我先手写探明机制，再用框架产品化"——后者更适合带前端 + 人机协同的下一阶段。

- **日期**：2026-07-08（迁移 + smoke 验证 + 全量 15 case 验证完成）
- **维度**：工程结构 / 可维护性（非诊断质量维度）
- **针对的 case**：全部 15 case（parity 验证需要全量，不能只看 smoke 4）
- **迁移前**：`doctor/src/graph/nodes/diagnosis_agent/react_loop.py`（175 行手写 ReAct `while` 循环），5 个机制内联在循环体里
- **迁移后**：`react_loop.py` 删除；新增 `doctor/src/graph/nodes/diagnosis_agent/middleware/` 包（7 文件 573 行）：

  | 文件 | 行数 | 对应的手写循环机制 | 用的钩子 |
  |---|---|---|---|
  | `run_context.py` | 70 | （新）`DiagnosisRunContext` + `ContextVar`——middleware 实例跨调用复用，per-invocation 状态走 ContextVar | — |
  | `langfuse_tracing.py` | 146 | start_trace / record_tool_span / tool token 记账 | `abefore_agent` + `awrap_model_call` + `awrap_tool_call` |
  | `budget_guard.py` | 75 | ContextBudget 计时/计 token/计 model call，超限 `jump_to: end` | `abefore_model` + `aafter_model` |
  | `tool_dedup.py` | 55 | 重复 tool call 跳过 + `record_tool_skipped` | `awrap_tool_call` |
  | `tool_truncation.py` | 88 | 长 tool 结果截断 + tool 异常兜底 | `awrap_tool_call` |
  | `forced_call.py` | 94 | agent 未自然交付结构化输出时强制 `with_structured_output` 收尾 | `aafter_agent` |
  | `__init__.py` | 45 | 包导出 | — |

  `node.py`（+203/-）改走 `agent.ainvoke` + ContextVar set/clear；`subgraphs/diagnosis_agent.py`（+38）把 5 个 middleware 接进 `create_agent`；`langfuse_tracing.py`（observability 层，+89/-）的 `on_tool_*` callback 改 no-op（见下方踩坑）。

- **为什么 5 个机制都能用 middleware 复刻**：迁移前的关键顾虑是"框架的 6 个钩子能不能做到手写循环的精确控制"。逐个核对后发现全都可以，映射关系如下：

  | 手写循环里的控制点 | 框架钩子 | 怎么做到 |
  |---|---|---|
  | 循环开始前 init budget / start_trace | `abefore_agent` | 在这里建 `ContextBudget`、`call_history`、调 `start_trace` |
  | 每次 LLM 调用前检查预算、超限 break | `abefore_model` | `model_call_count++` + tick，超限返回 `{"goto": "end"}`（middleware 的 `jump_to` 机制） |
  | 每次 LLM 调用后记 reasoning token | `aafter_model` | 从 `response` 取 usage 喂 `ContextBudget` |
  | 每个 tool call 前查重 / 截断 / 记 span | `awrap_tool_call` | 三层 middleware 嵌套 wrap（Dedup 外、Truncation 中、Langfuse 内），各管各的 |
  | 循环结束后如果没 JSON 就强制收尾 | `aafter_agent` | 检查 `final_messages` 末尾是不是 structured output，不是就触发 forced call |
  | 每个 LLM call 单独附 Langfuse callback | `awrap_model_call` | `request.model.with_config({"callbacks": [handler]})` 只给 model 附 callback |

  唯一手写循环有、middleware 没直接对应的，是"per-invocation 可变状态（budget / call_history / model_call_count）"——因为 middleware 实例是跨调用复用的单例。解法是 `DiagnosisRunContext` dataclass + `ContextVar`：`node.py` 在 `ainvoke` 前 `set_run_context(ctx)`，middleware 里 `get_run_context()` 读，`finally` 里 `clear_run_context()`。这是框架组件复用模式下的标准技巧，不是框架缺陷。

- **踩坑：Langfuse tool observation 双记录（process_quality 0.958 → 0.806）**

  这是本次迁移最值得记的坑，也是"框架不是免费午餐"的硬证据。

  - **现象**：第一轮 framework smoke（`framework-smoke-20260708-141807`，旧代码）`process_quality` 从 baseline 0.958 掉到 **0.806**。dump trace 发现每个 tool call 被记成 **2 个 span**（一个有 args、一个空 args），`score_process_quality` 把 phantom span 算进 tool 调用数，efficiency 分被拉垮。
  - **根因**：`create_agent` 的 `agent.ainvoke(config={"callbacks": [handler]})` 会把顶层 config 通过 **`RunnableConfig` contextvar** 透传给**所有**子节点——包括 model node **和** ToolNode。于是 Langfuse callback handler 的 `on_tool_start`/`on_tool_end` 对每个 tool fire 一次（empty-args span，因为 create_agent 不走 `on_agent_action`，`_last_tool_input` 没被设置），**同时** `LangfuseTracingMiddleware.awrap_tool_call` 又显式调 `record_tool_span`（有 args span）→ 每个 tool call 被双记录。
  - **第一次修复（部分生效）**：`node.py` 顶层 config 只传 `{"recursion_limit": 80}`，不再传 callbacks；改由 `LangfuseTracingMiddleware.awrap_model_call` 用 `model.with_config({"callbacks": [handler]})` 只给 model 附 callback。意图是让 ToolNode 继承一个 callback-less config。**但这没完全生效**——LangGraph 的 config contextvar 透传比预期广，model 上绑的 callback 仍会传播到 ToolNode，`on_tool_*` 仍在 fire。
  - **第二次修复（最终版）**：承认"追查 callback 为什么还能到 tool 没意义"，直接让 **callback 路径的 tool 记录全部 no-op**——`on_tool_start` / `on_tool_end` / `on_tool_error` / `on_agent_action` 全部 `return`，`record_tool_span`（middleware 显式调）成为 tool observation 的**唯一来源**，`_tool_call_idx` 计数器也由它独占。
  - **为什么对 baseline 安全**：hand-written loop 里 tool 是 `await tool.ainvoke(args)` **无 callback config** 调用的，`on_tool_*` 本来就 never fire（`record_tool_span` 的 docstring 白纸黑字写了"on_tool_start / on_tool_end callbacks never fire"）——所以 no-op 这 4 个方法对 baseline trace **零影响**，baseline 的 tool span 本来就全来自 `record_tool_span`。no-op 只是把"framework 下 callback 会额外 fire"这个差异抹平。
  - **验证**：`framework-smoke-v3`（`framework-smoke-20260708-145130`）`process_quality` 回到 **0.958**，逐 case 比特和 baseline 一致（BE-020 0.83 / FE-020 1.00 / LOGIC-020 1.00 / PERF-020 1.00）。dump FE-021 trace 确认每个 tool call 恰好 1 个 span、idx 连续无重复（v2 是 56 observations 双记录，v3 是 24 observations 单记录）。
  - **方法论启示**：框架的"自动透传"是把双刃剑——手写循环里"tool 不带 callback"是**显式的**（你不传就没有），框架里"tool 不带 callback"是**隐式的**（你得拦住透传），后者更容易踩坑。这个坑写成 iteration log 的一部分，比只展示"迁移成功"更诚实。

- **另一个已知 gap（诚实记录，未修）**：`awrap_model_call` 只给**第一次** model call 成功附上 callback，后续 model call 的 LLM generation observation 没被记录（dump 显示每个 case 只有 1 个 `llm_call_*` generation，baseline 是 N 个）。**不影响 `process_quality` 评分**（scorer 只看 tool span），但 Langfuse UI 里 LLM 调用链不完整。初判是 `request.model.with_config(...)` 的 mutation 不跨 iteration 持久 / create_agent 后续 iteration 走了原 model 引用。留作 follow-up——优先级低，因为评分维度不受影响，且修复方向明确（要么改回顶层 config + 靠 no-op 防 tool 双记录，要么在 `awrap_model_call` 里每次都重新 bind）。

- **全量 15 case 结果**（session=`framework-15case-v1-20260708-145926`，vs baseline-15case-v2=`baseline-15case-v2-20260707-144018`）：

  | recipe | framework overall | baseline overall | Δ | framework proc | baseline proc | 备注 |
  |---|---|---|---|---|---|---|
  | BE-020 | 0.97 | 0.97 | 0 | 1.00 | 0.83 | proc 升（这次自然 stop 更干净） |
  | BE-021 | 0.97 | 0.98 | -0.01 | 1.00 | 1.00 | parity |
  | BE-022 | 0.94 | 0.95 | -0.01 | 1.00 | 1.00 | parity |
  | CASCADE-020 | 0.72 | 0.79 | -0.07 | 1.00 | 1.00 | root 0.65→0.40，推理方差（L4 级联） |
  | CONFIG-020 | 0.97 | 0.97 | 0 | 0.83 | 0.83 | parity（forced_call 救活仍有效） |
  | DATA-020 | 0.96 | 0.96 | 0 | 1.00 | 1.00 | parity |
  | DATA-021 | 0.86 | 0.90 | -0.04 | 0.83 | 1.00 | cat=0.00 选错类（方差，同 baseline 历史） |
  | FE-020 | 0.70 | 0.73 | -0.03 | 1.00 | 1.00 | parity（fix=0.45 推理分歧，同 baseline） |
  | FE-021 | 0.62 | 0.97 | **-0.35** | 1.00 | 1.00 | **回归**：root 0.95→0.30，forced_call 触发了但推理走偏（见下） |
  | LOGIC-020 | 0.97 | 0.96 | +0.01 | 1.00 | 1.00 | parity |
  | LOGIC-021 | 0.97 | 0.97 | 0 | 1.00 | 1.00 | parity |
  | LOGIC-022 | 0.94 | 0.94 | 0 | 1.00 | 0.83 | proc 升 |
  | PERF-020 | 0.87 | 0.84 | +0.03 | 1.00 | 1.00 | parity |
  | PERF-021 | 0.92 | 0.92 | 0 | 1.00 | 1.00 | parity（forced_call 救活仍有效） |
  | RACE-020 | 0.92 | 0.86 | +0.06 | 1.00 | 1.00 | 升 |

  **聚合对照**（framework vs baseline，9 维度，Δ = framework − baseline）：

  | 维度 | framework | baseline | Δ | 判定 |
  |---|---|---|---|---|
  | overall | 0.887 | 0.914 | -0.027 | **parity**（在 ±0.03 方差带内） |
  | root_cause_accuracy | 0.843 | 0.929 | -0.086 | 回归（被 FE-021/CASCADE 单点拉低） |
  | fix_suggestion_quality | 0.911 | 0.913 | -0.002 | parity |
  | affected_file_accuracy | 1.000 | 0.933 | +0.067 | 升 |
  | affected_line_accuracy | 0.800 | 0.767 | +0.033 | 升 |
  | category_accuracy | 0.900 | 0.933 | -0.033 | parity（带内） |
  | evidence_chain_completeness | 0.880 | 0.899 | -0.019 | parity |
  | confidence_calibration | 0.869 | 0.950 | -0.081 | 回归（同 root，被单点拉低） |
  | **process_quality** | **0.978** | **0.967** | **+0.011** | **parity（略升）** |

- **回归归因（不是框架缺陷，是 LLM 推理方差）**：

  唯一超方差带的回归是 **FE-021（0.97→0.62）**。dump 它的 trace（17 observations）：`structured_output_ForcedDiagnosisReport` SPAN 存在 → **forced_call 触发了**，`root_cause_accuracy=0.30` → 不是交付失败（JSON 交付成功），是**这一轮推理本身走偏了**（agent 把根因归错了）。这正是 baseline 里 FE-021 自己的历史方差模式：iter0=0.04 / iter1-full=0.04 / iter2=0.97——它在 0.04 和 0.97 之间反复横跳，0.62 落在它的历史包络内。CASCADE-020（-0.07）同理，是 L4 级联 case 的推理不完整，baseline 历史也波动（0.68/0.79）。

  **关键判据**：root_cause / confidence 两个维度的均值回归，**完全由 FE-021 + CASCADE-020 两个单点贡献**；其余 13 个 case 全部在 parity。而 process_quality（衡量"怎么查"的维度，最不受 LLM 推理方差影响、最能反映框架迁移是否破坏了 agent 的查证方法）**持平且略升**。这说明框架迁移**没有破坏 agent 的查证流程**，overall 的 -0.027 落在方差带内、且方向上被两个已知高方差 case 拉低——不是系统性回归。

- **验收结论：parity 达成 ✓**

  - `process_quality` 0.978 vs 0.967（+0.011）——可观测性模型等价，tool observation 单源记录修复后比特一致
  - `overall` 0.887 vs 0.914（-0.027，在 ±0.03 方差带内）——非系统性回归
  - 5 个机制全部在框架里复刻且生效：forced_call 仍在救 disaster（CONFIG-020/PERF-021 维持 0.97/0.92）、tool 去重/截断/budget guard 通过 `awrap_tool_call` + `abefore_model` 工作、Langfuse trace 结构与 baseline 等价
  - 已知 gap：LLM generation observation 只记第 1 次（不影响评分，留 follow-up）

- **代码量对比（诚实记录：迁移不是"更少代码"，是"更结构化"）**：

  | | 手写循环 | 框架迁移后 |
  |---|---|---|
  | 核心 loop / middleware | `react_loop.py` 175 行（单文件，5 机制内联） | `middleware/` 7 文件 573 行（每机制独立文件，最大 146 最小 55） |
  | 新增测试 | — | `test_middleware.py` 454 行（含 `awrap_model_call` 单测） |
  | `node.py` | 调手写 loop | +203/- 改走 `ainvoke` + ContextVar |
  | **净效果** | 175 行单文件 | 573 行多文件 + 454 行测试 |

  **叙事要点**：框架迁移让代码**变多了**，不是变少了——这要诚实承认。但换来了：(1) 每个机制是独立可测的 middleware 类（单测隔离），(2) ReAct 循环本身交给框架维护（未来 LangChain 升级 loop 逻辑不用我们改），(3) 为下一阶段"前端 + 人机协同"提供了标准扩展点（人机协同可以做成第 6 个 middleware，挂 `before_agent` / `aafter_agent`，而不是再改 loop 本体）。**"先手写探明机制、再用框架产品化"是比"我手写了一个 agent loop"更高级的工程叙事**——前者证明你既懂底层又懂工程权衡，后者只证明你会手写。

- **测试**：`tests/graph/` 62 passed（含 5 个 middleware 各自单测 + `awrap_model_call` 单测 + 2 个 `TestForcedCallWiredIntoNode` 端到端用 `ScriptedChatModel` 验 forced_call 触发/cap 场景）。`TestForcedCallWiredIntoNode` 重写踩了两个 create_agent 行为坑：(1) `recursion_limit=30` 太低（create_agent 每个 middleware hook 是独立 graph step，一次 ReAct ≈ 4 步，12 次 LLM 调用需 ~52 步）→ 调到 80 让 BudgetGuard 的 `model_call_count` cap 成为实际约束；(2) scripted `ToolCall` dict 缺 `type: "tool_call"` 字段导致 ToolNode `_parse_input` 找不到 AIMessage → 补上 + tool_call_id 每轮唯一。

- **工具脚本新增**：
  - `scripts/fetch_experiment_scores.py`——按 session_id 分页拉 trace + 逐 trace 拉 score，输出 9 维度表 + 均值 + JSON 落盘
  - `scripts/dump_trace_observations.py`——dump 单个 trace（session_id + recipe_id）的所有 observation（name + type + args 预览），用于双记录诊断
  - `scripts/verify_middleware_assumptions.py`（throwaway，已删）——迁移前验证 3 个风险假设：`wrap_tool_call` 执行顺序、`after_agent` 能否追加 messages、Langfuse callback 透传范围

- **下一步**：parity 已达，迁移收尾。后续可选方向：
  1. 修 LLM generation observation 只记第 1 次的 gap（低优先级，不影响评分）
  2. 进入"前端 + 人机协同"阶段——第 6 个 middleware 挂 `aafter_agent`，agent 给出 DiagnosisReport 后暂停等人确认/补充，再 resume
  3. Iteration 3 推理维度优化（FE-020 file/line 归因分歧、CASCADE-020 根因不完整）——这些和框架无关，是 prompt / 推理维度，可独立于迁移推进

命令（复跑 / 复现）：
```
cd D:\Work\LearnAI\DiagDoctor\doctor
# 全量 15 case
uv run python scripts/eval_agent.py --run-name framework-15case-v1
# 取分
uv run python scripts/fetch_experiment_scores.py framework-15case-v1-<ts>
# dump 单 trace 确认 tool observation 单记录
uv run python scripts/dump_trace_observations.py framework-15case-v1-<ts> FE-021
# 测试
uv run pytest tests/graph/ -q
```
