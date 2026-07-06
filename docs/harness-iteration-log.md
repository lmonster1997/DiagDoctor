# DiagDoctor Harness 迭代优化日志

> 分支：`dev-harness-redesign`（从 `main` 拉出，含 2 个 LLM observability 修复 commit）
> 起始日期：2026-07-05
> 目的：把 DiagDoctor 诊断 Agent 的 harness 从「过度设计的多层防御」重做成「case 驱动的增量机制」。
> 用途：作为面试叙事材料——展示如何用证据驱动的方法做 harness 工程，而不是理论先行。

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

（待填）

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
| `scripts/run_baseline_experiment.py --session <sid>` | 跑 4 个 smoke case 的端到端评估，写入 Langfuse session |
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

---

## 下一次 iteration kickoff 备忘

> 每次开新会话窗口时，让 AI 先读这份文档，再从下面这段继续。

```
当前状态：Iteration 1 已完成（实现 + 单测 + smoke + 全量 15 case 验证）。
  机制：loop 结束后若 last AIMessage 不含可 parse JSON，做一次未 bind_tools 的额外 LLM call
        强制输出 JSON。两个 instruction 模板分别覆盖 mode 1（cap + 空 content）/ mode 2（narrative）。
  gate：_last_ai_has_json → 已含 JSON 的 case 跳过（零回归设计 + 方差防护副作用）。
  单测：tests/graph/test_forced_final_json_call.py 15 tests 全过；ruff clean。
  全量结果（session=baseline-15case-v1-20260706-131440，实质是 iter1-full）：
    mean=0.874（vs baseline v2 mean=0.666，Δ=+0.208，远超 ±0.03 方差带）
    disasters=1（PERF-021）——验收阈值（≤2、≥0.75）全部满足
    forced_call 触发 6/15：
      机制救活 3 个 disaster（CONFIG-020 / FE-020 / FE-021，forced_call=True 且 disaster→healthy）
      方差救活 2 个 disaster（BE-020 / BE-021，forced_call=False，agent 这次恰好自然交付 JSON）
      机制触发但救不了 1 个（PERF-021，forced call 输出干净 JSON 但诊断内容错——推理失败非交付失败）
      机制在 healthy case 触发 2 个无回归（LOGIC-021 / LOGIC-022，gate 设计的方差防护副作用）
    gate 正确跳过 9 个（已交付 JSON 的 case，零误判）

下一步：Iteration 2，针对 PERF-021 的新失败模式——「症状误读 + 锁死错误路径」。
  trace 显示 agent 在 LLM #4 看到 `GET /api/projects/` 只 2 个 SQL 查询（24ms+52ms）"并不慢"，
    却把症状偷换成"任务列表慢"，锁死在 PERF-020 的 N+1 pattern 上。
  LLM #12 注意到矛盾（代码有 selectinload 但 trace 显示 N+1）却解释成"Bug Factory 污染"而非"看错了 endpoint"。
  这是 Prompt / 推理维度问题，不是输出格式问题——forced call 救不了。
  候选机制（待 case 驱动验证，不要理论先行）：
    - 在 system prompt 加「症状锚定」约束：每轮复查 user_report 的关键词，路径选择必须回溯到症状
    - 检测"矛盾解释 away"行为：agent 用"污染/未生效"解释代码-trace 矛盾时，注入 nudge 让它复查 endpoint
    - 但要先跑几次 PERF-021 看是否稳定复现，确认不是方差
  验收阈值（Iteration 2）：PERF-021 root_cause_accuracy 从 0.00 提到 ≥0.85，且不破坏其他 14 case（特别是 already-healthy 的 12 个不能退步超过 ±0.03 方差带）。

命令（Iteration 2 节奏）：
  # 改完先跑 smoke（4 case，~5min）做 sanity check——确保没灾难性回归
  cd D:\Work\LearnAI\DiagDoctor\doctor
  uv run python scripts/run_baseline_experiment.py --split smoke --run-name iter2-smoke
  uv run python scripts/dump_session_scores.py iter2-smoke-<ts>
  uv run python scripts/dump_forced_call_flag.py iter2-smoke-<ts>

  # 决策点跑全量 15 + 与 baseline-15case-v1-20260706-131440 对比（iter1-full，mean=0.874）
  #   注意：必须看 PERF-021 是否被救活 + 其他 14 case 不退步
  uv run python scripts/run_baseline_experiment.py --run-name iter2-full
  uv run python scripts/dump_session_scores.py iter2-full-<ts>
  uv run python scripts/dump_forced_call_flag.py iter2-full-<ts>

  # 看 PERF-021 trace 验证症状锚定机制是否生效
  uv run python scripts/dump_trace_llm_responses.py --session <sid> --bug PERF-021 --show-input

注意：run_name 现在会自动补 timestamp 后缀（避免 Langfuse session 前缀聚合歧义）。
  显式传 --run-name iter2-foo → 实际 run_name = iter2-foo-20260705-2103
  已带 YYYYMMDD-HHMMSS 后缀的不再重复补。
```
