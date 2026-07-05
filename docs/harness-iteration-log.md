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

---

## 下一次 iteration kickoff 备忘

> 每次开新会话窗口时，让 AI 先读这份文档，再从下面这段继续。

```
当前状态：Iteration 0 baseline 已完成（2026-07-05，run=baseline-15case，overall mean=0.693，4 disaster）。
  失败模式定位（看 trace LLM call 序列验证过，不是猜的）：
    - mode 1（3/4 disaster，BE-020/FE-020/FE-021）：跑到 12 轮 cap，末轮 content="" → parse 空 → 空报告
    - mode 2（1/4 disaster，BE-021）：第 11 轮自然 stop，但末轮是 1376 字叙事散文，不是 JSON → parse 失败
  反直觉发现：易 case（BE-020/FE-020/FE-021）全崩，难 case（L3/L4 red-herring/smokeless）全过。
  根因：agent 推理能力够，问题在「不知道何时停止 + 停了也不输出 JSON 格式」。

下一步：Iteration 1，加「stop 后 forced final JSON call」——单一机制同时覆盖两种 failure mode。
  loop 结束后做一次额外 LLM call，prompt 强制「基于已收集证据输出 DiagnosisReport JSON，不要再调任何工具」。
  自然 stop 时把最后一条 narrative 喂进去做格式化；跑到 cap 时把 accumulated findings 喂进去做交付。

命令（三层节奏）：
  # Iteration 1 实现完后跑 train（8 case，~40min）——主战场
  cd D:\Work\LearnAI\DiagDoctor\doctor
  uv run python scripts/run_baseline_experiment.py --split train --run-name iter1-forced-json
  uv run python scripts/dump_session_scores.py iter1-forced-json-<ts>

  # 大改后先跑 smoke（4 case，~5min）做 sanity check
  uv run python scripts/run_baseline_experiment.py --split smoke --run-name iter1-smoke

  # 决策点跑全量 15 + 与 baseline-15case 对比
  uv run python scripts/run_baseline_experiment.py --run-name iter1-full
  uv run python scripts/dump_session_scores.py iter1-full-<ts>

  # 看具体 trace 的 LLM 输入输出（用于失败模式定位）
  uv run python scripts/dump_trace_llm_responses.py --session <sid> --bug <id> --show-input

注意：run_name 现在会自动补 timestamp 后缀（避免 Langfuse session 前缀聚合歧义）。
  显式传 --run-name iter1-foo → 实际 run_name = iter1-foo-20260705-2103
  已带 YYYYMMDD-HHMMSS 后缀的不再重复补。
```
